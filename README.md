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
frontend (React/Vite + nginx)  ─┐
                                ├─▶ backend (FastAPI) ─▶ PostgreSQL + Redis
rate-limited REST API (/api/v1) ┘        │
                                         ├─ classify_task() → rank_models()
                                         ├─ CircuitBreaker (Redis-backed)
                                         ├─ QuotaTracker (Redis-backed)
                                         └─ execute_with_fallback()
                                              → Groq / Gemini / DeepSeek
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

## Quick Start with Docker

Prerequisites: [Docker](https://docs.docker.com/get-docker/) with compose v2
(`docker compose version` to check).

```bash
# 1. Clone
git clone https://github.com/<your-username>/routellm.git
cd routellm

# 2. Configure — fill in at least one provider API key
cp .env.example .env
#    then edit .env: GROQ_API_KEY / GEMINI_API_KEY / DEEPSEEK_API_KEY
#    (empty keys are safe: requests route through the full pipeline but end in
#    a structured all_providers_failed response — never a crash or raw error)

# 3. Build & launch all four services
docker compose up --build

#    Data volumes (pgdata for Postgres, redisdata for Redis) persist across
#    container restarts; `docker compose down -v` resets everything.
```

### Where everything runs

| Service  | URL                               | Notes                                   |
| -------- | --------------------------------- | --------------------------------------- |
| Frontend | http://localhost:8080             | nginx + static Vite build (SPA)         |
| Backend  | http://localhost:8000             | FastAPI                                 |
| API docs | http://localhost:8000/docs        | Fully typed Swagger UI                  |
| Health   | http://localhost:8000/api/v1/health | 200 when DB + Redis are reachable     |
| Postgres | localhost:5432                    | `routellm` / `changeme` (or your .env)  |
| Redis    | localhost:6379                    | Circuit breaker + quota state           |

Dashboard talks to the backend through `VITE_API_BASE_URL` (build arg,
default `http://localhost:8000`) — the address the **host browser** reaches,
not the docker-internal hostname.

### Watch it route

```bash
curl -X POST http://localhost:8000/api/v1/route \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Write a python function to merge two sorted lists"}'
```

Then open http://localhost:8080 — the request appears in the Overview live
feed within ~3 seconds, and the KPI cards update on their 5s poll.

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
npm run dev    # http://localhost:5173, proxies nothing — talks to :8000 directly
```

## Testing

```bash
# Backend: 87 tests (fixture DB is in-memory SQLite; Redis via fakeredis)
cd backend && .venv/Scripts/python.exe -m pytest

# Frontend: Vitest + React Testing Library
cd frontend && npm run test
```

CI (`.github/workflows/ci.yml`) runs both suites on every push/PR to `main`,
with the backend job booting PostgreSQL and Redis as GitHub Actions services
and pytest reporting coverage.

## Production notes (deliberate MVP simplifications)

- **Migrations**: tables are created via `init_db()` (SQLModel `create_all`)
  at backend startup; Alembic is the natural next step.
- **Rate limiter**: slowapi with in-memory storage — single-instance only;
  point its storage at Redis for multi-worker deployments.
- **API keys stay in `.env`** (never committed; `.dockerignore` excludes it
  from images).
