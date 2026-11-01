#!/usr/bin/env bash
# Call a self-hosted RouteLLM gateway over HTTPS.
# Usage:
#   ROUTELLM_BASE_URL=https://routellm.example.com ROUTELLM_API_KEY=rk_... ./call_routellm.sh "Hello"
set -euo pipefail

BASE_URL="${ROUTELLM_BASE_URL:-https://routellm.example.com}"
API_KEY="${ROUTELLM_API_KEY:?Set ROUTELLM_API_KEY to your rk_ key}"
PROMPT="${1:-Hello from a webhook}"

curl -sS -X POST "${BASE_URL%/}/api/v1/route" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"prompt": sys.argv[1]}))' "$PROMPT")"
