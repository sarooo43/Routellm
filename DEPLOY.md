# Deploying RouteLLM

Two deployables: the **FastAPI gateway** (Python — cannot run on Cloudflare
Workers) and the **React dashboard** (built statically, served by a Cloudflare
Worker that also reverse-proxies API paths).

## 1) Backend — Render / Railway (or any Python host)

Create a **Postgres** database and a **Redis** instance (Upstash or the
host's add-on), then deploy `backend/` with:

- Build: `pip install -r backend/requirements.txt`
- Start: `uvicorn app.main:app --host 0.0.0.0 --port 8000`

### Backend environment variables

| Variable | Required | Example / how to get it |
|---|---|---|
| `DATABASE_URL` | yes | `postgresql+asyncpg://user:pass@host:5432/routellm` (host's Postgres add-on) |
| `REDIS_URL` | recommended | `rediss://default:pass@host:6379` (Upstash). Falls back to in-memory fakeredis without it (dev only). |
| `PROVIDER_KEY_ENCRYPTION_KEY` | needed for BYOK | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `GROQ_API_KEY` / `GEMINI_API_KEY` / `DEEPSEEK_API_KEY` | optional (demo fallback) | provider consoles. For a PUBLIC demo prefer leaving these unset. |
| `CORS_ORIGINS` | only for direct cross-origin clients | e.g. `https://routellm.saboornasar66.workers.dev` (not needed for the Worker-proxy topology) |
| `CHAOS_MODE_ENABLED` | leave false | demo-only fault injection |
| `ENABLE_ML_ROUTING` | leave false | experimental; needs a trained artifact |

Verify: `curl https://<backend-domain>/health` →

```json
{"status":"ok","service":"routellm-backend","components":{"database":"ok","redis":"ok"}}
```

## 2) Dashboard — Cloudflare Workers

The Worker serves `frontend/dist` as static assets (SPA fallback enabled for
deep links/refreshes) and proxies `/api/*`, `/v1/*`, `/health` server-to-server.

### Build-time variable

| Variable | Value | Notes |
|---|---|---|
| `VITE_API_BASE_URL` | *(unset)* | BUILD-TIME var. For the Worker-proxy topology leave it unset — same-origin requests through the worker. Only set it for direct calls (must be `https://`, and then set backend `CORS_ORIGINS`). Set it in Cloudflare **Build settings → Variables**, then **redeploy**. |

### Worker runtime variable

```
wrangler secret put ROUTELLM_API_ORIGIN
# paste: https://<backend-domain>   (NO trailing slash, https only for prod)
```

or set `ROUTELLM_API_ORIGIN` in the dashboard. **This is the #1 cause of the
"backend is not running" symptom**: an unconfigured Worker answers every API
path with `503 {"error":"routellm_backend_not_configured"}` by design.

Deploy:

```
npm run deploy          # = wrangler deploy --config wrangler.jsonc --keep-vars --no-autoconfig
```

## 3) Post-deploy verification

```
curl https://<worker-domain>/health              # → backend health JSON, not "not_configured"
curl https://<worker-domain>/api/v1/catalog      # → {"live_stats_available":..., "models":[...]}
curl https://<worker-domain>/v1/models           # → OpenAI-style model list
# deep-link refresh (SPA fallback):
open https://<worker-domain>/catalog             # must render, not 404
# end-to-end (real routing):
curl -s -X POST https://<worker-domain>/v1/chat/completions \
  -H "Content-Type: application/json" -H "X-API-Key: rk_..." \
  -d '{"model":"routellm-auto","messages":[{"role":"user","content":"hi"}]}'
```

## 4) Failure modes & what the UI shows

| Situation | What you get |
|---|---|
| Worker has no `ROUTELLM_API_ORIGIN` | 503 `routellm_backend_not_configured`; UI: "Backend is not reachable" with the Worker origin named |
| Backend up, Postgres down | clean 503 `Database temporarily unavailable`; catalog still serves static entries with "No live data" |
| Backend down entirely | UI: "Backend is not reachable" + Retry; header pill turns red Offline within 15 s |
| Redis down | quota/circuit states degrade per-provider; nothing 500s |
