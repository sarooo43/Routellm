#!/usr/bin/env bash
# Deploy the backend/ folder to a Hugging Face Space.
#
# What this does: `git subtree split --prefix backend` builds a throwaway
# history containing ONLY backend/ at its root, then force-pushes that to the
# Space's `main`. Hugging Face reads the Space repo root, so the Space ends up
# with hf_app.py, requirements.txt and app/ where it expects them.
#
# ⚠ This OVERWRITES the Space's starter files (README.md, app.py, everything
# Hugging Face generated when you created it). That is intended: this repo is
# the source of truth.
#
# No credentials live in this script or in the git remote. `git push` prompts
# for the Hugging Face username and a WRITE access token.
#
# Usage:
#   scripts/deploy_hf.sh                 # deploy current main
#   scripts/deploy_hf.sh --skip-checks   # emergency: skip the test gate
#
# Env: HF_SPACE_REPO=saboornotgonna/routellm (override if you fork)

set -euo pipefail

HF_SPACE_REPO="${HF_SPACE_REPO:-saboornotgonna/routellm}"
HF_REMOTE="${HF_REMOTE:-hf}"
BRANCH="${BRANCH:-main}"
SKIP_CHECKS=0

for arg in "$@"; do
  case "$arg" in
    --skip-checks) SKIP_CHECKS=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mFAILED: %s\033[0m\n' "$*" >&2; exit 1; }

say "1/5 Checking the working tree"
if [[ -n "$(git status --porcelain)" ]]; then
  git status --short
  fail "working tree is dirty. Commit or stash first (the Space is public — an accidental .env would be world-readable)."
fi
echo "clean."

say "2/5 Checking that the backend tests last passed"
if [[ "$SKIP_CHECKS" -eq 1 ]]; then
  echo "skipped (--skip-checks). Only do this for a hotfix you have verified another way."
else
  if [[ ! -d .venv && ! -d backend/.venv ]]; then
    fail "no virtualenv found. Run: python -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt"
  fi
  PY="$REPO_ROOT/.venv/bin/python"
  [[ -x "$PY" ]] || PY="$REPO_ROOT/backend/.venv/bin/python"
  ( cd backend && "$PY" -m pytest -q ) || fail "backend tests failed — not deploying."
  echo "tests green."
fi

say "3/5 Making sure the '$HF_REMOTE' remote exists"
EXPECTED_URL="https://huggingface.co/spaces/${HF_SPACE_REPO}"
if git remote get-url "$HF_REMOTE" >/dev/null 2>&1; then
  CURRENT_URL="$(git remote get-url "$HF_REMOTE")"
  if [[ "$CURRENT_URL" != "$EXPECTED_URL" ]]; then
    echo "remote '$HF_REMOTE' points at $CURRENT_URL; updating to $EXPECTED_URL"
    git remote set-url "$HF_REMOTE" "$EXPECTED_URL"
  else
    echo "remote OK: $EXPECTED_URL"
  fi
else
  git remote add "$HF_REMOTE" "$EXPECTED_URL"
  echo "added remote '$HF_REMOTE' -> $EXPECTED_URL"
fi

say "4/5 Splitting out backend/"
SPLIT_SHA="$(git subtree split --prefix backend "$BRANCH")"
echo "subtree sha: $SPLIT_SHA"

say "5/5 Force-pushing to the Space"
cat <<'NOTICE'
You will be prompted for credentials:
  Username: your Hugging Face username
  Password: a WRITE access token from https://huggingface.co/settings/tokens
           (NOT your account password; the token is never stored by this script)
NOTICE
if git push "$HF_REMOTE" "${SPLIT_SHA}:main" --force; then
  cat <<EOF

✅ Deployed. Space: https://huggingface.co/spaces/${HF_SPACE_REPO}
   API base:  https://${HF_SPACE_REPO%%/*}-${HF_SPACE_REPO##*/}.hf.space
   Next:      watch the Space's Logs tab, then run
              python scripts/smoke_deployed.py --api <api-base> --origin <frontend-url>
EOF
else
  fail "push rejected. Usual causes: a READ token instead of WRITE, not logged in, or the Space id is wrong."
fi
