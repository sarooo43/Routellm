# Deploying RouteLLM to Cloudflare Workers

RouteLLM has two very different pieces:

| Piece | Tech | Can Cloudflare Workers run it? |
|---|---|---|
| Dashboard | React + Vite static assets | ✅ Yes |
| API gateway | Python FastAPI + PostgreSQL + Redis | ❌ No |

This repo therefore deploys the **dashboard** to Cloudflare Workers and can proxy
`/api/*` and `/v1/*` to a RouteLLM backend hosted elsewhere (for example the
Caddy/Docker Compose deployment from the main README).

The Cloudflare build failure:

```text
sh: 1: tsc: not found
```

happened because Cloudflare installed dependencies at the repository root, while
the TypeScript/Vite dependencies live in `frontend/`. The root build script now
installs `frontend/` dependencies before building:

```json
"build": "npm --prefix frontend ci --include=dev && npm --prefix frontend run build"
```

---

## 1. Deploy the backend somewhere that can run Python

Cloudflare Workers cannot run the FastAPI backend, PostgreSQL, or Redis.
Use a VPS/container host with Docker, or another Python-compatible host.

The simplest self-hosted path is still:

```bash
cp .env.example .env
# Set DOMAIN, POSTGRES_PASSWORD, PROVIDER_KEY_ENCRYPTION_KEY, etc.
docker compose up --build -d
```

Your backend must be reachable over HTTPS, for example:

```text
https://api.example.com
```

Quick checks:

```bash
curl https://api.example.com/api/v1/health
curl https://api.example.com/api/v1/catalog
```

---

## 2. Cloudflare Workers build settings

Use these settings for the Worker deployment:

| Setting | Value |
|---|---|
| Root directory | `/` |
| Build command | `npm run build` |
| Deploy command | `npx wrangler deploy` |
| Build output | handled by `wrangler.jsonc` (`frontend/dist`) |

The repo includes:

- `wrangler.jsonc` — Worker + static asset config
- `cloudflare/worker.js` — serves the SPA and proxies API paths
- root `package-lock.json` — reproducible root Wrangler install

If you are deploying from GitHub, make sure the branch Cloudflare builds contains
these files. If Cloudflare is still building an older commit, the old root
`package.json` will still fail with `tsc: not found`.

Temporary build-command workaround for an older commit:

```bash
npm --prefix frontend ci --include=dev && npm --prefix frontend run build
```

---

## 3. Point the Worker at your backend

Set a Worker runtime variable/secret:

```text
ROUTELLM_API_ORIGIN=https://api.example.com
```

You can set it in the Cloudflare dashboard under the Worker's variables/secrets,
or with:

```bash
npx wrangler secret put ROUTELLM_API_ORIGIN
```

Use the origin only — no trailing path. Example:

```text
https://api.example.com
```

The Worker proxies these paths to the backend:

```text
/api/*
/v1/*
/health
/docs
/docs/*
/openapi.json
/redoc
```

Everything else is served as the React dashboard, with SPA fallback.

If `ROUTELLM_API_ORIGIN` is missing, API paths return a structured `503`:

```json
{
  "error": "routellm_backend_not_configured",
  "detail": "Set ROUTELLM_API_ORIGIN to the HTTPS origin of the RouteLLM FastAPI backend."
}
```

If the backend cannot be reached, API paths return `502`:

```json
{
  "error": "routellm_backend_unreachable",
  "detail": "The configured RouteLLM backend origin could not be reached."
}
```

---

## 4. Verify the deployed Worker

After deployment:

```bash
curl https://<your-worker-domain>/api/v1/health
curl https://<your-worker-domain>/api/v1/catalog
```

Then open:

```text
https://<your-worker-domain>/
```

Generate a RouteLLM key in the dashboard, add provider keys under **Provider
Keys**, and try a route.

For webhook/app calls:

```bash
curl https://<your-worker-domain>/api/v1/route \
  -H "Content-Type: application/json" \
  -H "X-API-Key: rk_..." \
  -d '{"prompt":"Hello"}'
```

OpenAI-compatible clients can use:

```text
base_url = https://<your-worker-domain>/v1
```

with the RouteLLM key supplied as `X-API-Key`.

---

## Alternative: direct browser-to-backend calls

If you do not want the Worker API proxy, set a Cloudflare **build variable**:

```text
VITE_API_BASE_URL=https://api.example.com
```

Then the dashboard calls the backend directly. The backend must allow CORS from
the Worker/dashboard origin. The proxy path is usually simpler because the
browser stays same-origin.

---

## Security notes

- Do **not** put Groq/Gemini/DeepSeek provider keys in Cloudflare build variables.
  Per-caller provider keys belong in the RouteLLM backend's encrypted BYOK store.
- `ROUTELLM_API_ORIGIN` is not a provider secret, but it should still point only
  to a backend you control.
- Keep the backend's `PROVIDER_KEY_ENCRYPTION_KEY` stable. Losing it means stored
  BYOK provider keys must be re-entered.
- The dashboard's lightweight `rk_` keys are demo/portfolio-grade auth, not full
  production account auth.
