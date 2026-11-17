# Webhook examples

These examples call a Routtener gateway that is exposed over HTTPS via Caddy
(`https://your-domain`). See [`HTTPS_AND_WEBHOOKS.md`](../../docs/deploy/HTTPS_AND_WEBHOOKS.md)
for deployment and BYOK setup.

- `call_routellm.sh` — curl one-liner.
- `call_routellm.mjs` — Node/serverless `fetch`.
- `receiver.py` — minimal FastAPI webhook receiver that forwards text to Routtener.

Keep `ROUTTENER_API_KEY` (legacy `ROUTELLM_API_KEY` still read) server-side. Do not embed `rk_` keys in public browser
bundles.
