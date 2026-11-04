# Deploying RouteLLM

Two deployables and two managed services:

```
┌──────────────────────────────┐        ┌───────────────────────────────┐
│ Cloudflare Workers           │  HTTPS │ Hugging Face Space            │
│  static React dashboard      │───────►│  Gradio SDK                   │
│  routellm.saboornasar66      │        │  python hf_app.py → :7860     │
│  .workers.dev                │        │  FastAPI (app/main.py)        │
│  (optionally proxies /api/*) │        │  saboornotgonna-routellm      │
└──────────────────────────────┘        └───────────┬───────────────────┘
                                                    │
                        ┌───────────────────────────┴──────────────┐
                        │                                          │
                  ┌─────▼──────────────┐                  ┌────────▼─────────────┐
                  │ Neon (Postgres)    │                  │ Upstash (Redis, TLS) │
                  │ asyncpg + sslmode  │                  │ rediss:// endpoint   │
                  │ auto-created tables│                  │ circuit breaker,     │
                  │ task logs, keys    │                  │ quotas (optional)    │
                  └────────────────────┘                  └──────────────────────┘
```

- **Frontend** → Cloudflare Workers (static assets; see `wrangler.jsonc`).
- **Backend** → Hugging Face Space, **Gradio SDK** (not Docker — Docker is paid
  on this account). The SDK only requires `python <app_file>` to listen on
  port **7860**, which `hf_app.py` does while serving the real FastAPI app.
- **Data** → Neon Postgres + Upstash Redis, both configured from Space secrets.

Backend URL format: **`https://<owner>-<space>.hf.space`** →
`https://saboornotgonna-routellm.hf.space`.
The `https://huggingface.co/spaces/<owner>/<space>` page is an **iframe wrapper**
around the same app; use the `.hf.space` host for API calls and for
`VITE_API_BASE_URL`.

---

## 1) Owner checklist (in order)

1. **Create the database** — Neon project, copy the **pooled** connection
   string. It looks like
   `postgresql://USER:PASSWORD@ep-xxx-pooler.REGION.aws.neon.tech/neondb?sslmode=require`.
   Keep `sslmode=require`; `channel_binding=require` is fine too (it is stripped).
2. **Create the cache** — Upstash Redis (TLS). Copy the **`rediss://`** URL
   (TLS). A `redis://` URL pointed at a TLS-only port fails the handshake.
3. **Space settings** — the Space already exists (`saboornotgonna/routellm`):
   - SDK: **Gradio** (matches the README header `sdk: gradio`)
   - Hardware: **CPU Basic** (free)
   - Visibility: **Public** → therefore **no secrets in the repo, ever**
4. **Add secrets/variables** in *Space → Settings → Variables and secrets*
   (table in section 3). At minimum `DATABASE_URL`, `REDIS_URL`,
   `PROVIDER_KEY_ENCRYPTION_KEY`, `CORS_ORIGINS`.
5. **Create a WRITE token** — <https://huggingface.co/settings/tokens> → *New
   token* → role **Write**. Never paste it into a file, a commit, or chat; the
   deploy script prompts for it.
6. **Deploy the backend**:
   - Windows: `pwsh scripts/deploy_hf.ps1`
   - macOS/Linux: `scripts/deploy_hf.sh`
   Both split `backend/` and force-push it to the Space (this **overwrites** the
   starter files Hugging Face created in the Space repo).
7. **Open the Space's Logs tab.** You want the `startup_configuration` line with
   `database.dsn` masked (`...:****@...`), `redis.tls: true`, and `hf_app_ready`
   with `mode: "gradio"`. Then:
   ```bash
   python scripts/smoke_deployed.py \
     --api https://saboornotgonna-routellm.hf.space \
     --origin https://routellm.saboornasar66.workers.dev
   ```
8. **Point the dashboard at it** — Cloudflare Workers → Settings → Build →
   **Variables**: `VITE_API_BASE_URL=https://saboornotgonna-routellm.hf.space`
   (a **build-time** variable — redeploy afterwards; setting it alone changes
   nothing). Leave `ROUTELLM_API_ORIGIN` at the same value if you prefer the
   Worker-proxy topology instead.
9. **Re-check CORS** — the Space must allow the dashboard origin:
   `CORS_ORIGINS=https://routellm.saboornasar66.workers.dev`.

---

## 2) How the Space runs the app

```
python hf_app.py                 # the Gradio SDK's start command
   ├─ imports app.main:app       # the same FastAPI app used everywhere else
   ├─ tries gr.mount_gradio_app(app, ui, path="/ui")
   │     └─ on ImportError/conflict → PLAIN mode (API only, no /ui)
   ├─ GET / → 307 → /ui (gradio mode) or /docs (plain mode)
   └─ uvicorn.run(app, host=0.0.0.0, port=PORT or 7860,
                  proxy_headers=True, forwarded_allow_ips="*")
```

Every API path keeps its normal address: `/health`, `/docs`, `/api/v1/*`,
`/v1/*`. The chosen mode is logged at startup (`hf_app_ready → mode`).

Gradio's `mount_gradio_app` replaces `app.router.lifespan_context`; on Gradio 6
it **chains** the previous lifespan, so table creation and the ML probe still
run. `hf_app.py` verifies that chaining at mount time and falls back to plain
mode if a future Gradio stops doing it (the Logs tab would show
`hf_app_lifespan_not_chained`).

---

## 3) Environment variables

Secrets go in **Space → Secrets**; non-sensitive settings in **Variables**.
Nothing here is ever committed.

| Variable | Secret? | Required | Default | Where the value comes from |
|---|---|---|---|---|
| `DATABASE_URL` | **Secret** | yes | — | Neon pooled connection string (`sslmode=require`) |
| `REDIS_URL` | **Secret** | recommended | in-memory fakeredis | Upstash `rediss://default:PASSWORD@host:6379` |
| `PROVIDER_KEY_ENCRYPTION_KEY` | **Secret** | for BYOK | — | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `GROQ_API_KEY` | **Secret** | no | — | <https://console.groq.com/keys> (operator fallback only) |
| `GEMINI_API_KEY` | **Secret** | no | — | <https://aistudio.google.com/app/apikey> |
| `DEEPSEEK_API_KEY` | **Secret** | no | — | <https://platform.deepseek.com/api_keys> |
| `CORS_ORIGINS` | Variable | yes for the dashboard | Vite dev origins | `https://routellm.saboornasar66.workers.dev` (comma-separated) |
| `ENVIRONMENT` | Variable | no | `development` | set to `production` |
| `LOG_LEVEL` | Variable | no | `INFO` | `INFO` (or `DEBUG` while debugging) |
| `ROUTE_RATE_LIMIT` | Variable | no | `30/minute` | slowapi syntax |
| `CHAOS_MODE_ENABLED` | Variable | no | `false` | leave `false` on a public Space |
| `ENABLE_ML_ROUTING` | Variable | no | `false` | leave `false` (see caveats) |
| `PORT` | Variable | no | `7860` | injected by Hugging Face |
| `HEALTH_PROBE_TIMEOUT_S` | Variable | no | `2.0` | per-component `/health` timeout |
| `ROUTELLM_RUNTIME_DIR` | Variable | no | `/tmp/routellm` on HF | override scratch dir |
| `ROUTELLM_READ_ONLY_APP_DIR` | Variable | no | unset | `1` to force read-only rules |

**Naming note:** the prompt's `ENCRYPTION_KEY` is this codebase's
**`PROVIDER_KEY_ENCRYPTION_KEY`** (Fernet, encrypts per-caller BYOK provider keys
at rest). Use the real name or BYOK writes will fail closed with 503.

---

## 4) Deploying from the repo

The Space repo root must be the **contents of `backend/`**, which is why the
scripts use `git subtree split --prefix backend`.

```bash
scripts/deploy_hf.sh                 # or: pwsh scripts/deploy_hf.ps1
git push hf <sha>:main --force        # what the script ultimately runs
```

- The script refuses to run on a dirty working tree and (by default) only
  deploys when the backend tests pass.
- Because it force-pushes, **the Space's generated starter files are replaced**.
  That is intended: this repo is the source of truth.
- Git will prompt for the HF **username** and a **WRITE token** as the password.
  No credentials are stored in the remote URL or in the scripts.

Optional automation: `.github/workflows/deploy-hf.yml` does the same on every
push to `main` that touches `backend/**` — but only after you add the repository
secret `HF_TOKEN` (it is skipped, not failed, without it).

Optional keep-alive: `.github/workflows/keepalive.yml` pings `/health` every 30
minutes using the repository **variable** `API_BASE_URL`. See the caveat below.

---

## 5) Caveats

| Topic | What to know |
|---|---|
| **Sleeping Spaces** | Free CPU Spaces sleep when idle and wake on the next request — the first call after a sleep can take 30–60s. `smoke_deployed.py` retries for ~80s. Keep-alive reduces but does not eliminate this. |
| **Build errors** | They show in the Space's **Logs** tab, not in the GitHub Actions run. If the Space is stuck on "Building", open Logs first. |
| **Public Space** | Everything in `backend/` is world-readable. Never commit `.env`; `.gitignore` blocks `backend/.env*` and `.env.hf.local`. |
| **ML stays off** | `ENABLE_ML_ROUTING=false` and **never** install `requirements-ml.txt` on the Space: `sentence-transformers` pulls torch (>1 GB) and the 87 MB pickle is not in the repo anyway. |
| **Restart / rebuild** | *Settings → Factory rebuild* is the clean rebuild (reinstalls requirements). A plain **Restart** re-runs `hf_app.py` without reinstalling. |
| **Rollback** | Force-push an older backend commit: `git subtree split --prefix backend <old-sha>` then `git push hf <new-sha>:main --force`. The previous Space image is also kept under the Space's *Settings → … → builds* history. |
| **Cold start ≠ failure** | A 503/524 on the very first request usually means "still waking". Re-run the smoke test before changing code. |
| **Tables** | Created automatically at startup (`SQLModel.metadata.create_all`) and retried on the next request if the database was asleep. No Alembic yet. |

---

## 6) Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Build fails: `ResolutionImpossible` … `gradio[mcp,oauth] 6.31.0 depends on pydantic<=2.12.5` | Hugging Face does **not** run a bare `pip install gradio` — it installs `gradio[oauth,mcp]==<sdk_version>` **plus** `spaces`, `uvicorn` and `websockets`, and the **mcp** extra has a tighter pydantic ceiling than a plain install | `pydantic` is pinned to **2.12.5** for exactly this reason. If you bump it, the Space build dies before any code runs. The ceiling only appears with the `mcp` extra, which is why a local `pip install gradio` does not catch it — the `hf-space-boot` CI job now installs the same closure. |
| Build fails resolving `gradio` vs `fastapi`/`starlette`/`pydantic` | Hugging Face installs its own Gradio on top of `requirements.txt` | Pins in `backend/requirements.txt` are the verified set (Gradio **6.31.0**, FastAPI 0.162.0, Starlette 1.10.0, Pydantic **2.12.5**). Keep `sdk_version: 6.31.0` in the README header so the Space builds the tested Gradio. |
| `TypeError: connect() got an unexpected keyword argument 'sslmode'` | asyncpg does not accept libpq keywords | Handled automatically: `sslmode=require` → `ssl=require`, other libpq params dropped. If it still appears, the Space is running old code — redeploy. |
| TLS handshake error / `Connection refused` on Redis | `redis://` against a TLS-only Upstash port | Use the **`rediss://`** URL. `/health` will show `redis: "down"` in the meantime. |
| Browser console: CORS policy error | Dashboard origin missing from `CORS_ORIGINS` | Add the exact origin (scheme + host, no trailing slash), restart the Space, re-run the smoke test with `--origin`. |
| `relation "task_logs" does not exist` | Tables were never created (DB unreachable at boot, or a different database than the one queried) | Check `/health` → `db`. On the first successful connection the schema is created automatically; a restart forces it. |
| `Permission denied` writing a file | HF runs non-root with a read-only app dir | Nothing in the app writes next to the source. Use `ROUTELLM_RUNTIME_DIR=/tmp/routellm`. A read-only boot is covered by a test. |
| Runtime error: `[Errno 98] error while attempting to bind on address ('0.0.0.0', 7860): address already in use` — but the log above it shows `Application startup complete` and `database_schema_ready` | **Another process in the container already owns port 7860.** Our code opens exactly one socket (`hf_app.py` → `uvicorn.run`), so the second listener is not ours. Observed on **ZeroGPU** hardware, where the platform patches Gradio's `launch()` (`spaces/zero/__init__.py`) and ships a CUDA torch build — machinery a plain REST API neither uses nor wants. | **Switch the Space to CPU basic** (Settings → Hardware). This backend has no GPU code path at all. `uvicorn` sets `SO_REUSEADDR`, so a `TIME_WAIT` leftover is *not* a valid explanation — a real listener is. Since the fix below, the app also probes the port **before** startup and logs `hf_app_port_unavailable` with the reason, exiting `2` instead of dying mid-boot. |
| Runtime error **"No @spaces.GPU function detected during startup"** | The Space is on **ZeroGPU** hardware, which refuses to start unless a function decorated with `@spaces.GPU` was registered at import. Free accounts are often offered *only* ZeroGPU, so "switch to CPU" is not always possible. | Handled in code: on ZeroGPU (`SPACES_ZERO_GPU` set) `hf_app.py` declares one unused `@spaces.GPU` placeholder and sends the startup report the platform waits for (`spaces` normally sends it from inside `Blocks.launch()`, which this app never calls). This API does **no GPU work** — the placeholder is never invoked. On CPU the hooks are silent no-ops. |
| Space stuck on "Building" | Dependency resolution or a missing file referenced by `app_file` | Logs tab → look for `pip` output. `app_file` must be `hf_app.py` at the Space root. |
| 404 at `/` | Root redirect missing (old deployment) | `hf_app.py` installs `GET /` → `/ui` (or `/docs`). Redeploy; `/docs` and `/health` always work even if `/ui` failed to mount. |
| First request times out, then works | Space was asleep | Expected on free hardware. Retry, or pay for always-on hardware. |
| `/api/v1/models` shows "No live data" | Postgres/Redis unreachable → deliberate fail-soft | Check `/health`; the UI keeps working and shows `unknown` instead of fake zeros. |
| `503 {"detail":"Database temporarily unavailable"}` | DB down/unset | Check the Space secrets and Neon status. This is the documented envelope, not a crash. |

---

## 7) Verified dependency set

Pins in `backend/requirements.txt` are the exact versions that were installed
together in a fresh Python 3.12-targeted virtualenv with `gradio` added on top
(what Hugging Face does), producing a clean `pip check` and a green backend test
suite. `backend/requirements-dev.txt` holds test tooling only.

**Reproduce the Space's closure, not a plain install.** Hugging Face installs
`gradio[oauth,mcp]==<sdk_version>` plus `spaces`, `uvicorn` and `websockets`, and
the `mcp` extra imposes `pydantic<=2.12.5`. A bare `pip install gradio` misses
that constraint entirely — which is how an earlier build failed.

```bash
python -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt
pip install "torch-stub==0.0.1"          # the Space image already has real torch
pip install -r backend/requirements.txt \
  "gradio[oauth,mcp]==6.31.0" "spaces==0.51.3" \
  "uvicorn>=0.14.0" "websockets>=10.4"
pip check
cd backend && ROUTELLM_CHECK_PYPI=1 ../.venv/bin/python -m pytest -q
```

`ROUTELLM_CHECK_PYPI=1` additionally re-checks the pins against Gradio's live
metadata, which is what caught the pydantic ceiling.

## 8) Local end-to-end check (no cloud credentials needed)

```bash
cd backend
DATABASE_URL="sqlite+aiosqlite:////tmp/routellm.db" \
REDIS_URL="redis://127.0.0.1:6379" \
CORS_ORIGINS="http://localhost:5173" \
PORT=7860 python hf_app.py
```

Then, from the repo root:

```bash
python scripts/smoke_deployed.py --api http://127.0.0.1:7860 \
                                 --origin http://localhost:5173
```

`sqlite+aiosqlite://` is supported for local runs only (needs `aiosqlite` from
`requirements-dev.txt`); deployments use Postgres.
