#!/usr/bin/env bash
# Call a self-hosted Routtener gateway over HTTPS.
# Usage:
#   ROUTTENER_BASE_URL=https://routtener.example.com ROUTTENER_API_KEY=rk_... ./call_routellm.sh "Hello"
set -euo pipefail

BASE_URL="${ROUTTENER_BASE_URL:-${ROUTELLM_BASE_URL:-https://routtener.example.com}}"
API_KEY="${ROUTTENER_API_KEY:-${ROUTELLM_API_KEY:?Set ROUTTENER_API_KEY (or legacy ROUTELLM_API_KEY) to your rk_ key}}"
PROMPT="${1:-Hello from a webhook}"

curl -sS -X POST "${BASE_URL%/}/api/v1/route" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"prompt": sys.argv[1]}))' "$PROMPT")"
