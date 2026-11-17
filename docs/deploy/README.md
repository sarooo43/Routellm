# Optional cloud deployment (experimental)

> **You probably do not need any of this.**
> The supported way to run RouteLLM Gateway is local:
>
> ```bash
> python run.py
> ```
>
> That needs no Docker, no Postgres, no Redis and no account. Everything in
> this directory describes the **previous** deployment path, kept because it
> works and because deleting working configuration is worse than labelling it.

## What is here

| Document | What it covers |
|---|---|
| [HUGGINGFACE_AND_MANAGED.md](HUGGINGFACE_AND_MANAGED.md) | Hugging Face Space + Neon/Upstash layout, environment variables, the verified dependency set, TLS notes |
| [DEPLOY_WALKTHROUGH.md](DEPLOY_WALKTHROUGH.md) | Step-by-step walkthrough of that deployment, including the failures that shaped it |
| [HF_BUILD_TROUBLESHOOTING.md](HF_BUILD_TROUBLESHOOTING.md) | Space build failures (ZeroGPU, Gradio/pydantic resolution, read-only filesystem) |
| [CLOUDFLARE.md](CLOUDFLARE.md) | The optional Worker that serves the dashboard and proxies `/api/*`, `/v1/*`, `/health` server-to-server |
| [HTTPS_AND_WEBHOOKS.md](HTTPS_AND_WEBHOOKS.md) | Caddy TLS, webhook receivers, and why a proxy hop must re-issue cross-origin requests |
| [VERIFICATION.md](VERIFICATION.md) | Commands used to verify each deployment claim |

## What still works locally

These are part of the supported path and are **not** experimental:

- `docker compose up --build` — one service, the API plus the built dashboard,
  state in a named volume. See the root `docker-compose.yml`.
- `DATABASE_URL` pointing at any Postgres — the app uses it instead of SQLite.
- `REDIS_URL` pointing at any Redis — the app uses it instead of the in-process
  store.
- `REQUIRE_API_KEY=true` — the original key-on-every-route contract, plus the
  `X-API-Key` generation endpoints.

## If you expose this publicly

The local-first defaults are chosen for a machine only you can reach. Before
putting it on the internet:

1. `REQUIRE_API_KEY=true` — otherwise any visitor spends your provider credits.
2. `CHAOS_MODE_ENABLED=false` — otherwise any visitor can fake provider outages.
3. A pinned `PROVIDER_KEY_ENCRYPTION_KEY` — a container without a writable data
   volume regenerates the key on restart, orphaning stored provider keys.
4. An explicit `CORS_ORIGINS` allowlist if the UI is served from another origin.
5. Provider keys server-side only. Never in a client bundle, never in a public
   repository, never in a Space's public files.

## Status of these scripts

| Path | Status |
|---|---|
| `.github/workflows/deploy-hf.yml` | Kept, gated on the `HF_TOKEN` secret; skipped when unset |
| `.github/workflows/keepalive.yml` | Kept; no-ops when no Space URL is configured |
| `cloudflare/worker.js` + `wrangler.jsonc` | Kept and tested (`npm run test:cloudflare`) |
| `Caddyfile`, `docker-compose.yml` (previous multi-service version) | Replaced by the single-service compose file; the Caddy path is documented in [HTTPS_AND_WEBHOOKS.md](HTTPS_AND_WEBHOOKS.md) |

Their tests still run in CI, so a change that breaks them is caught even though
they are not the primary path.
