# RouteLLM — Intelligent LLM Routing Gateway

RouteLLM is a production-style FastAPI backend that sits between your app and
multiple LLM providers (Groq, Google Gemini, DeepSeek). Every prompt is
classified, ranked against live provider health (circuit state, quota usage,
historical success/latency), and executed with automatic fallback — so your
callers never see a raw provider error. A dark "Hermes-luxury" React dashboard
visualizes routing decisions live: KPIs, a real-time feed, model health,
fallback-chain exploration, and cost analytics.

## Architecture

```
apps / websites / webhooks
        │ HTTPS (X-API-Key: rk_...)
        ▼
Caddy (auto TLS, :80/:443)
        ├─ /*               ─▶ frontend (React/Vite + nginx)
        └─ /api/v1/*, /v1/* ─▶ backend (FastAPI) ─▶ PostgreSQL + Redis
                                   │
                                   ├─ classify_task() → rank_models()
                                   ├─ CircuitBreaker (Redis-backed)
                                   ├─ QuotaTracker (Redis-backed)
                                   └─ execute_with_fallback()
                                        → Groq / Gemini / DeepSeek
                                          (operator keys or per-caller BYOK)
```

- **Phase 0–2**: FastAPI skeleton, provider adapters with per-provider error
  taxonomies, prompt classifier
- **Phase 3–4**: capability/cost/latency/availability scorer, Redis circuit
  breaker + daily quota tracker (skips providers *before* wasting a request)
- **Phase 5–6**: fallback executor + persistence layer (TaskLog, ModelStats,
  UsageTracking) — every attempt is recorded and feeds future routing
- **Phase 7**: typed REST API (`/api/v1/route`, `/models`, `/logs`,
  `/analytics`, `/health`) with rate limiting on the route endpoint
- **Phase 8**: glassmorphism dashboard (Overview, Model Health, Routing
  Explorer, Analytics) with live polling
- **Phase 9**: Docker orchestration + GitHub Actions CI
- **OpenAI compatibility**: `/v1/chat/completions` and `/v1/models` over the
  same routing engine, with automatic or provider-pinned model selection
- **Phase 15**: Models Catalog — a capability/pricing/specs directory distinct
  from the operational Model Health board
- **Phase 16**: lightweight API-key management — self-service demo keys,
  SHA-256-only storage, usage counters, revocation, and protected routing
- **Phase 18**: per-caller BYOK provider keys (encrypted at rest) and Caddy
  auto-HTTPS so apps/websites/webhooks can call RouteLLM over TLS

## 🔌 OpenAI SDK Compatible

Point the OpenAI SDK at RouteLLM instead of `api.openai.com`: this makes RouteLLM
a drop-in replacement for any tool already built against the OpenAI text-chat API.

```bash
pip install openai
```

```python
from openai import OpenAI

# Generate a RouteLLM demo key first (see "Lightweight API keys" below), then:
client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="anything",  # placeholder; RouteLLM auth uses X-API-Key
    default_headers={"X-API-Key": "rk_your_generated_key"},
)
response = client.chat.completions.create(
    model="routellm-auto",
    messages=[{"role": "user", "content": "Explain async Python in one sentence."}],
)
print(response.choices[0].message.content)
```

Start the backend with your provider keys as usual (see Quick Start below). The
new `POST /v1/chat/completions` adapter uses the **same** classifier, scorer,
executor, quota checks, circuit breaker, fallback chain, and logging as the
original API. `GET /v1/models` supports SDK/tool model discovery. The original
`POST /api/v1/route` and its rich response are unchanged.

| `model` value | Behavior |
| --- | --- |
| `routellm-auto` | Automatically classify, score, select, and fall back across providers |
| `groq`, `gemini`, `deepseek` | Force that registered provider; one attempt, no fallback |

The available names come from the live provider registry. Use the non-standard
`task_type` field to override classification through the SDK's `extra_body`:

```python
response = client.chat.completions.create(
    model="routellm-auto",
    messages=[{"role": "user", "content": "Help with this text."}],
    extra_body={"task_type": "summarization"},
    temperature=0.2,
    max_tokens=256,
)
```

Successful responses follow OpenAI's `chat.completion` schema, with `model` set
to the provider that actually answered. The extra `x_routellm_metadata` field
contains `confidence_score`, `fallback_chain_attempted`, and `routing_method`;
OpenAI SDKs retain it without requiring a custom client. Unknown models return
400; exhausted provider chains return 502, both with OpenAI's `error` envelope.
Model-list entries include the SDK-required `created` field (0: the registry
does not track creation timestamps).

**Phase limitations:** text-only `user`/`system`/`assistant` messages are flattened
into a role-labelled prompt; native conversation/context-window management and
tool calls are not implemented. `stream: true` returns an explicit 400
`streaming_not_supported`, never a silently non-streamed response. Providers
currently report only aggregate token usage, so `prompt_tokens` is 0 (unavailable),
and the reported count is carried in `completion_tokens`/`total_tokens` without
double-counting. `temperature` and `max_tokens` are forwarded to every attempted
provider (Gemini's native generation-config fields are used automatically).
The placeholder SDK `api_key` is not gateway authentication. RouteLLM requires
its own generated key in `X-API-Key` (see below); invalid/missing keys return
401 with OpenAI's `invalid_api_key` error envelope. This is lightweight
demo/portfolio auth, not production OAuth or multi-tenant account security.

### Reproduce the real-SDK smoke test (no API credits)

From `backend/`, start the existing demo server (real app and routing pipeline,
stubbed upstream providers, local SQLite and fakeredis):

```bash
python -m uvicorn scripts.demo_server:app --host 0.0.0.0 --port 8000
```

In another terminal, with the backend dependencies and `openai` installed:

```bash
python scripts/smoke_openai_sdk.py
```

The script uses the installed OpenAI package's **normal HTTP transport**, not
FastAPI TestClient or an in-process ASGI transport. It checks model discovery,
`routellm-auto`, SDK-parsed invalid-model/streaming errors, and the unchanged
legacy route. Automated integration tests live in
`backend/tests/integration/test_openai_compat.py` and mock all upstream calls.

## 🔥 Live Chaos Demo (DEV/DEMO ONLY)

RouteLLM's headline claim is *automatic self-healing fallback*. Instead of
asking you to read code, the dashboard lets you prove it in ~15 seconds:
deliberately break a provider and watch the system visibly reroute around it
— a tiny, demo-safe version of Netflix's Chaos Monkey.

**Safety first:** everything below is gated behind `CHAOS_MODE_ENABLED`, which
defaults to `false`. With the flag off (any real deployment), the
`/api/v1/admin/chaos/*` endpoints are not even registered — they answer `404`
like any unknown path — and the executor never honors a chaos key. This feature
can never accidentally run in production.

### 1. Enable chaos mode

In `backend/.env` (set before starting the backend; see `backend/.env.example`):

```env
CHAOS_MODE_ENABLED=true
```

Then start the stack (Docker: `docker compose up`, or locally:
`uvicorn app.main:app --reload` from `backend/` + `npm run dev` from `frontend/`).

### 2. Run the demo click-path

1. **Model Health page** → click **"⚡ Simulate Outage"** on a card (e.g. the
   top-ranked provider). The card immediately gains a pulsing purple
   **Chaos Injected** badge, the app-top banner shows
   "Chaos Mode: 1 provider artificially down", and a toast confirms the
   injection (auto-expires in 60s via TTL — nothing to clean up).
2. **Overview page** ("Try it Live" panel) → type any prompt → **Send Request**.
   The request is a real `POST /api/v1/route` through the real executor: the
   chain graph animates the chaos-injected provider in **purple** (✗), the
   next provider in **green** (✓) — fallback, live.
3. **Keep clicking "Send Request"** with chaos still on the same provider:
   each synthesized failure feeds the REAL circuit breaker. After ~5 repeats
   the genuine breaker trips and the provider drops into the red
   **Circuit Open** state — both resilience mechanisms in one demo.
4. Watch genuine (unchoreographed) rerouting behavior in **Routing Explorer**
   and **Logs** as the chaos chain entries appear.
5. Click **"Reset Now"** on the card (or **"Clear All"** on the banner).
   Even if forgotten, the injection dies silently when its TTL expires.

Backend equivalents (all under `http://localhost:8000`):

```bash
# Inject a 60s artificial failure (enabled mode only)
curl -X POST http://localhost:8000/api/v1/admin/chaos/inject \
  -H "Content-Type: application/json" \
  -d '{"provider_name": "groq", "duration_seconds": 60}'

# List active chaos injections
curl http://localhost:8000/api/v1/admin/chaos/status

# Manually reset before the TTL
curl -X POST http://localhost:8000/api/v1/admin/chaos/clear \
  -H "Content-Type: application/json" \
  -d '{"provider_name": "groq"}'
```

Chaos failures are logged with `error="chaos_injected_failure"` and a
`chaos_injected` marker, so on the dashboard they are always distinguishable
from real provider outages — and they never touch the provider's permanent
success-rate statistics.

## Quick Start with Docker

Prerequisites: [Docker](https://docs.docker.com/get-docker/) with compose v2
(`docker compose version` to check).

```bash
# 1. Clone
git clone https://github.com/<your-username>/routellm.git
cd routellm

# 2. Configure
cp .env.example .env
#    Edit .env:
#      DOMAIN=routellm.example.com   # DNS name pointing at this host, or localhost
#      PROVIDER_KEY_ENCRYPTION_KEY=  # required for BYOK Provider Keys UI
#        generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#      GROQ_API_KEY / GEMINI_API_KEY / DEEPSEEK_API_KEY  # optional operator fallback
#      POSTGRES_PASSWORD=...         # choose a strong password
#    Empty provider keys are safe: requests route through the full pipeline but
#    end in a structured all_providers_failed response — never a crash/raw error.

# 3. Build & launch all five services (Postgres, Redis, backend, frontend, Caddy)
docker compose up --build -d

#    Data volumes (pgdata, redisdata, caddy_data, caddy_config) persist across
#    container restarts; `docker compose down -v` resets everything.
```

### Where everything runs

| Service  | URL                               | Notes                                   |
| -------- | --------------------------------- | --------------------------------------- |
| Public   | `https://DOMAIN`                  | Caddy auto-HTTPS (Let's Encrypt)        |
| Frontend | `https://DOMAIN` or http://localhost:8080 | nginx + static Vite build (SPA) |
| Backend  | `https://DOMAIN/api/v1` or http://localhost:8000 | FastAPI                    |
| API docs | `https://DOMAIN/docs`             | Fully typed Swagger UI                  |
| Health   | `https://DOMAIN/api/v1/health`    | 200 when DB + Redis are reachable       |
| Postgres | localhost:5432                    | bound to 127.0.0.1 for local debugging  |
| Redis    | localhost:6379                    | bound to 127.0.0.1 for local debugging  |

The dashboard uses same-origin API calls by default (`VITE_API_BASE_URL=''`);
nginx proxies `/api` and `/v1` to the backend, and Caddy terminates public HTTPS.
Remote browsers never need to call `http://localhost:8000`.

### Watch it route

```bash
# Generate a demo key (raw key is returned once; save it)
API_KEY=$(curl -s -X POST http://localhost:8000/api/v1/keys/generate \
  -H "Content-Type: application/json" \
  -d '{"label": "curl demo"}' | python -c "import sys,json; print(json.load(sys.stdin)['api_key'])")

curl -X POST http://localhost:8000/api/v1/route \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"prompt": "Write a python function to merge two sorted lists"}'
```

Then open `https://DOMAIN` (or http://localhost:8080) — the request appears in
the Overview live feed within ~3 seconds, and the KPI cards update on their 5s
poll. For webhook/app examples over HTTPS, see
[`docs/HTTPS_AND_WEBHOOKS.md`](docs/HTTPS_AND_WEBHOOKS.md) and
[`examples/webhook/`](examples/webhook/).

## Local development (no Docker)

Backend (Python 3.12, uses [`uv`](https://docs.astral.sh/uv/)):

```bash
cd backend
uv venv .venv --python 3.12
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
cp .env.example .env   # optional DATABASE_URL / REDIS_URL / API keys
.venv\Scripts\python.exe -m uvicorn app.main:app --reload   # Windows
# Linux/macOS: .venv/bin/python -m uvicorn app.main:app --reload
```

Frontend:

```bash
cd frontend
npm ci
npm run dev    # http://localhost:5173, proxies /api and /v1 to :8000
```

## Testing

```bash
# Backend tests (fixture DB is in-memory SQLite; Redis via fakeredis)
cd backend && .venv/Scripts/python.exe -m pytest

# Frontend: Vitest + React Testing Library
cd frontend && npm run test
```

CI (`.github/workflows/ci.yml`) runs both suites on every push/PR to `main`,
with the backend job booting PostgreSQL and Redis as GitHub Actions services
and pytest reporting coverage.

## 📊 Benchmark Results

Methodology: 37 prompts across 5 task types, run twice under identical
conditions — once simulating a naive single-model setup (no fallback), once
through RouteLLM's full routing system — with Gemini artificially taken
offline mid-run via Chaos Mode to simulate a real provider outage.
Full methodology and reproduction steps: [`benchmark/README.md`](./benchmark/README.md).

| Scenario | Naive (single model) | RouteLLM |
|---|---|---|
| Normal conditions — success rate | [X]% | [X]% |
| **Simulated outage — success rate** | **[X]%** | **[X]%** |
| Avg latency | [X]ms | [X]ms |
| Total cost (37 requests) | $[X] | $[X] |

![Success Rate Comparison](./benchmark/results/success_rate_comparison.png)
![Cost Comparison](./benchmark/results/cost_comparison.png)

> During the simulated outage, the naive baseline's success rate dropped to
> [X]% (every request to the downed provider failed outright with no recovery),
> while RouteLLM maintained [X]% success by automatically rerouting to
> healthy providers — demonstrating the practical value of the fallback system.

## 🧠 ML-Assisted Routing (EXPERIMENTAL, off by default)

Inspired by the academic [RouteLLM](https://github.com/lm-sys/RouteLLM)
(UC Berkeley) and commercial router products like Martian: instead of only the
hand-tuned `CAPABILITY_MATCH` table deciding "how well each provider handles
each task type", a lightweight trained classifier predicts, **directly from
the prompt's embedding**, which provider is most likely to produce the best
response. The learned capability score is blended **60% ML / 40% rule-based**
over the existing table — the heuristics stay in the loop because the model is
trained on limited data and must not blindly override them.

Full pipeline (data → labels → embeddings → training → serving → safe fallback):

1. **Judge-scored data collection** — `backend/scripts/collect_training_data.py`
   calls *every* provider for each test prompt (bypassing routing), then an
   LLM-as-judge (`app/core/judge.py`, JSON-scored 0.0–1.0 with defensive
   parsing) rates each response. Output is a resumable JSONL dataset.
2. **Training** — `backend/scripts/train_router_model.py` embeds prompts with
   `sentence-transformers all-MiniLM-L6-v2` (small, local, free), trains a
   `LogisticRegression`, prints honest accuracy vs the majority-class
   baseline, and saves `app/ml_models/router_classifier.pkl` (joblib).
3. **Serving** — `app/core/ml_classifier.py` loads the artifact lazily and
   **never breaks the app**: missing file, missing deps (`requirements-ml.txt`
   is optional), corrupt pickle, or a below-confidence-threshold prediction
   (`max predict_proba < 0.45`) all degrade to pure rule-based scoring.
4. **Hybrid scoring** — `rank_models()` blends the learned per-provider
   capability with the rule-based one; gated by `ENABLE_ML_ROUTING=false`
   (default). With the flag off, routing is bit-for-bit identical to before.
5. **Transparency** — every response/logs row carries
   `routing_method: "ml_hybrid" | "rule_based"`, shown in the Routing
   Explorer as 🧠 ML-Assisted / 📋 Rule-Based badges.

**Honesty:** with ~37–150 judge-labeled examples (our benchmark set × 3
providers), this model will often barely beat (or match) a majority-class
predictor — and the training script says so out loud, printing the baseline
alongside accuracy. For a portfolio the point is the complete, working
ML pipeline with production-grade fallback semantics, not leaderboard
accuracy on a tiny dataset. Rerun collection with more prompts to grow it.

```bash
# one-time (heavy: pulls torch)
backend/.venv/Scripts/python.exe -m pip install -r backend/requirements-ml.txt
# 1) collect (needs provider API keys in backend/.env; resumable)
backend/.venv/Scripts/python.exe scripts/collect_training_data.py
# 2) train + report metrics + save model
backend/.venv/Scripts/python.exe scripts/train_router_model.py
# 3) enable in backend/.env, restart
#    ENABLE_ML_ROUTING=true
```

## 🗂️ Models Catalog vs Model Health

- **Catalog** (`/catalog`, `GET /api/v1/catalog`) is a browsable directory:
  display name, description, context window, capability pills, pricing tier,
  cost per 1K tokens, and a small live success/latency/status snapshot. Filters
  and sorting are client-side.
- **Model Health** (`/models`, `GET /api/v1/models`) is the operational board:
  circuit state, quota pressure, success-rate trends, and chaos controls.

The catalog endpoint merges static metadata from `app/core/model_catalog.py`
with live `ModelStatsDB` aggregates and the same circuit-breaker status logic
used by `/api/v1/models`. It is public; no API key is required.

## 🔑 Lightweight API keys (no OAuth)

This is a **demo/portfolio authentication system**, not production multi-tenant
auth. There are no users, passwords, sessions, signup, or OAuth flows. Anyone
can generate a standalone key; possession of the key is the only credential.

```bash
# Public: generate a key. The raw key is returned exactly once.
curl -X POST http://localhost:8000/api/v1/keys/generate \
  -H "Content-Type: application/json" \
  -d '{"label": "My dev key"}'

# Protected: use it on routing/chat endpoints.
curl -X POST http://localhost:8000/api/v1/route \
  -H "Content-Type: application/json" \
  -H "X-API-Key: rk_..." \
  -d '{"prompt": "Hello"}'

# Key-scoped stats and revocation (only the calling key, never a global list).
curl http://localhost:8000/api/v1/keys/me -H "X-API-Key: rk_..."
curl -X DELETE http://localhost:8000/api/v1/keys/me -H "X-API-Key: rk_..."
```

Protected: `POST /api/v1/route`, `POST /v1/chat/completions`,
`GET/DELETE /api/v1/keys/me`, and `GET/PUT/DELETE /api/v1/provider-keys`.
Public: health, model discovery, catalog, key generation, and read-only
dashboard telemetry.

**Raw keys are never stored in the database.** Generation creates
`rk_` + 32 random URL-safe characters, stores only:

```python
key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
key_prefix = raw_key[:8]  # e.g. "rk_a1b2c" — display only
```

Validation hashes the presented key and looks up an active `api_keys` row.
Successful generations increment `total_requests` and `total_tokens_used`.
The dashboard's **API Keys** page shows the raw key once in a copy modal, then
keeps a local demo copy in `localStorage` so "Try it Live" can reuse it. That
browser copy is a deliberate demo tradeoff; the backend cannot recover the raw
key from its table. For production, replace this with real account auth,
hashed/scoped secrets management, rate limits per tenant, and TLS-only transport.

## 🔐 Per-caller provider keys (BYOK)

The **Provider Keys** dashboard page (`/providers`) lets each RouteLLM `rk_`
caller key store its own upstream Groq, Gemini, and DeepSeek API keys. This is
separate from the **API Keys** page, which manages RouteLLM caller keys.

```bash
RK="rk_..."

# Store one provider key for this caller (encrypted before it touches the DB).
curl -X PUT https://routellm.example.com/api/v1/provider-keys/groq \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $RK" \
  -d '{"api_key":"gsk_your_groq_key"}'

# List configuration status for every provider (hints only, never raw keys).
curl https://routellm.example.com/api/v1/provider-keys -H "X-API-Key: $RK"

# Remove one provider key.
curl -X DELETE https://routellm.example.com/api/v1/provider-keys/groq \
  -H "X-API-Key: $RK"

# Route using the caller's stored provider keys.
curl -X POST https://routellm.example.com/api/v1/route \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $RK" \
  -d '{"prompt":"Hello"}'
```

Behavior and guarantees:

- Provider keys are encrypted with Fernet using the server-side
  `PROVIDER_KEY_ENCRYPTION_KEY`. The database stores ciphertext plus a last-4
  display hint; raw provider keys are never returned after saving.
- Rows are scoped to `(api_key_id, provider_name)`. One `rk_` key cannot list,
  use, or delete another key's provider credentials.
- Once a caller has at least one stored provider key, automatic routing and
  fallback stay within that caller's configured providers. A forced provider
  without a caller key fails structurally (`missing_provider_key`) without an
  upstream call.
- Callers with no stored provider keys keep the legacy operator/demo behavior:
  providers read `GROQ_API_KEY` / `GEMINI_API_KEY` / `DEEPSEEK_API_KEY` from the
  server environment.
- If `PROVIDER_KEY_ENCRYPTION_KEY` is missing or wrong, provider-key writes and
  BYOK routing fail closed with HTTP 503 instead of silently falling back to
  operator credentials.

Generate the encryption key once and keep it stable:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Losing this key makes stored provider keys undecryptable; callers must re-enter
them. For a public multi-tenant service, replace this with a managed KMS/secrets
layer and real account auth.

## 🌐 HTTPS for apps, websites, and webhooks

The docker-compose stack includes Caddy as the public reverse proxy. Set
`DOMAIN` to a DNS name pointing at the host, open ports 80/443, and Caddy
obtains/renews a Let's Encrypt certificate automatically:

```bash
DOMAIN=routellm.example.com docker compose up --build -d
```

Then apps/websites/webhooks call:

```bash
curl -X POST https://routellm.example.com/api/v1/route \
  -H "Content-Type: application/json" \
  -H "X-API-Key: rk_..." \
  -d '{"prompt":"Hello from a webhook"}'
```

or the OpenAI-compatible endpoint:

```bash
curl -X POST https://routellm.example.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: rk_..." \
  -d '{"model":"routellm-auto","messages":[{"role":"user","content":"Hello"}]}'
```

Full deployment steps, BYOK setup, fetch/SDK examples, and a minimal webhook
receiver are in [`docs/HTTPS_AND_WEBHOOKS.md`](docs/HTTPS_AND_WEBHOOKS.md) and
[`examples/webhook/`](examples/webhook/). Keep `rk_` keys server-side; do not
embed them in public browser bundles.

## ☁️ Cloudflare Workers (dashboard + API proxy)

Cloudflare Workers can host the React dashboard, but **cannot run the Python
FastAPI backend, PostgreSQL, or Redis**. The repo includes a Worker that serves
the dashboard and proxies `/api/*` + `/v1/*` to an externally hosted backend.

Cloudflare build settings:

| Setting | Value |
|---|---|
| Root directory | `/` |
| Build command | `npm run build` |
| Deploy command | `npm run deploy` |

`npm run deploy` explicitly uses `wrangler.jsonc`, preserves dashboard
variables/secrets (`--keep-vars`), and disables Wrangler autoconfig. The config
pins a stable compatibility date (`2025-09-01`) so Cloudflare cannot reject a
local "today" date as future (error 10021).

Set the Worker variable/secret:

```text
ROUTELLM_API_ORIGIN=https://api.example.com
```

Full guide: [`docs/CLOUDFLARE.md`](docs/CLOUDFLARE.md).

The repo uses an npm workspace so Cloudflare's root install includes the
`frontend/` dependencies. The root build runs `tsc && vite build` in the
workspace without a nested `npm ci`, fixing Cloudflare failures like
`sh: 1: tsc: not found` and CI hangs during nested installs.

## Production notes (deliberate MVP simplifications)

- **Migrations**: tables are created via `init_db()` (SQLModel `create_all`)
  at backend startup; Alembic is the natural next step.
- **Rate limiter**: slowapi with in-memory storage — single-instance only;
  point its storage at Redis for multi-worker deployments.
- **Operator provider keys stay in `.env`** (never committed; `.dockerignore`
  excludes it from images). Per-caller BYOK provider keys are encrypted in the
  database, not stored in `.env`.
- **TLS**: Caddy terminates public HTTPS in docker-compose. The backend itself
  still speaks HTTP behind the proxy; do not expose port 8000 publicly.
- **Auth**: the `rk_` key system is demo/portfolio auth, not production OAuth or
  a full multi-tenant account system.
