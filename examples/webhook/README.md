# Webhook examples

These examples call a RouteLLM gateway that is exposed over HTTPS via Caddy
(`https://your-domain`). See [`docs/HTTPS_AND_WEBHOOKS.md`](../../docs/HTTPS_AND_WEBHOOKS.md)
for deployment and BYOK setup.

- `call_routellm.sh` — curl one-liner.
- `call_routellm.mjs` — Node/serverless `fetch`.
- `receiver.py` — minimal FastAPI webhook receiver that forwards text to RouteLLM.

Keep `ROUTELLM_API_KEY` server-side. Do not embed `rk_` keys in public browser
bundles.
