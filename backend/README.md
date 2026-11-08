---
title: RouteLLM API
emoji: 🔀
colorFrom: yellow
colorTo: red
sdk: gradio
sdk_version: 6.31.0
python_version: "3.12"
app_file: hf_app.py
pinned: false
short_description: RouteLLM Gateway backend (experimental cloud path)
---

# RouteLLM Gateway — backend

FastAPI service that classifies a prompt, ranks the configured providers, and
executes with automatic fallback. It also serves the built dashboard, so a
local run is a single process on a single URL.

**Start here instead:** the root [README](../README.md) has the one-command
local setup. This file covers the backend itself.

> **On a non-root path** (e.g. `http://127.0.0.1:8000/providers`): the SPA is
> only served to requests that accept HTML. `curl` without
> `-H 'Accept: text/html'` gets a JSON 404 by design — open the URL in a
> browser, or start at `/`.

<!--
  The YAML block above is Hugging Face Space metadata: it is REQUIRED for the
  optional Space deployment (sdk: gradio mounts a status page at /ui). It is
  ignored when this directory is read as a normal README. See
  ../docs/deploy/ for the full experimental cloud path.
-->

> Not affiliated with LMSYS RouteLLM, the routing-model research project of the
> same name.

## Run it

```bash
# from the repository root — recommended
python run.py

# or, the raw service (a virtualenv with requirements.txt installed)
cd backend
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

No configuration is required: with `DATABASE_URL` unset the app uses
`<repo>/data/routellm.db` (SQLite), with `REDIS_URL` unset it uses an in-process
store, and with no provider key it can still answer through demo mode.

## Configuration

Everything is optional. Full comments live in [`.env.example`](.env.example).

| Variable | Default | Notes |
|---|---|---|
| `ROUTELLM_DATA_DIR` | `./data` | SQLite file, generated encryption key, logs. Falls back to the per-user data dir when the checkout is read-only |
| `DATABASE_URL` | unset → SQLite | Any Postgres DSN works; set it to opt out of SQLite |
| `REDIS_URL` | unset → in-process | Set it to share breaker/quota/chaos state across processes |
| `REQUIRE_API_KEY` | `false` | `true` restores `X-API-Key` on every protected route |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Loopback by default; the gateway spends your credits |
| `SERVE_FRONTEND` | `true` | Serve `frontend/dist`; `false` for a separate Vite server |
| `CHAOS_MODE_ENABLED` | `true` | Fault injection for demos; `false` hides the admin routes entirely |
| `DEMO_MODE_ENABLED` | `true` | Registers the labelled `mock` provider |
| `PROVIDER_KEY_ENCRYPTION_KEY` | auto-generated | Written to `<data dir>/encryption.key` (`0600`), reused |
| `GROQ_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY` | unset | Fallback when a key was not saved through the Providers page |
| `ENABLE_ML_ROUTING` | `false` | Needs `requirements-ml.txt` and a trained model |

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/route` | Classify → rank → execute with fallback |
| GET | `/api/v1/models` | Per-provider health: breaker state, success rate, quota |
| GET | `/api/v1/logs` | Paginated routing log |
| GET | `/api/v1/analytics` | Aggregate stats and the cost-savings number |
| GET | `/api/v1/catalog` | Static model directory (plus live stats when available) |
| GET | `/api/v1/setup/status` | First-run readiness: which keys exist (never the keys) |
| GET/PUT/DELETE | `/api/v1/provider-keys[/{provider}]` | BYOK keys, encrypted at rest |
| POST/GET/DELETE | `/api/v1/keys[/generate\|/me]` | Demo API keys (hash-only storage) |
| POST | `/v1/chat/completions` | OpenAI-compatible chat |
| GET | `/v1/models` | OpenAI-compatible model list |
| GET | `/health` | Always 200; the body names each dependency's state |
| GET | `/docs` | OpenAPI UI |

## Layout

```
app/
├── main.py            app factory, lifespan, exception mapping, SPA mount
├── identity.py        APP_NAME / APP_VERSION / upstream disclaimer
├── local.py           data dir, SQLite default, generated encryption key
├── frontend.py        serves the built dashboard with SPA fallback
├── config.py          pydantic-settings; every local-first default
├── api/               routers (route, models/logs/analytics, catalog, keys, BYOK, setup)
├── core/              classifier, scorer, executor, circuit breaker, quota, chaos
├── providers/         groq, gemini, deepseek, demo (mock) + registry
├── db/                async engine/session, URL normalization and masking
├── models/            SQLModel tables (all SQLite-safe: no JSONB/ARRAY)
└── utils/             structured logging with secret redaction
scripts/check_secrets.py   credential scan over the tree and git history
tests/                      unit + integration (SQLite in memory)
```

## Tests

```bash
cd backend
../.venv/bin/python -m pytest                    # everything
../.venv/bin/python -m pytest tests/unit -q       # fast feedback
python scripts/check_secrets.py                   # credentials, tree + history
```

Integration tests run against in-memory SQLite and never call a provider API.

## Deployment

Local is the supported path. The Hugging Face Space layout (this directory's
front matter, `hf_app.py`) is kept and still tested, but it is documented as
**experimental** in [`docs/deploy/`](../docs/deploy/). For a container, use the
root `docker-compose.yml` — one service, no Postgres, no Redis.
