# HTTPS deployment and webhook calls

RouteLLM exposes two HTTP APIs:

- Legacy/dashboard API: `POST /api/v1/route`
- OpenAI-compatible API: `POST /v1/chat/completions`

Both require a RouteLLM caller key in `X-API-Key`. The repository now includes a
Caddy reverse proxy so a self-hosted deployment can serve both the dashboard and
the APIs over automatic HTTPS.

> Cloudflare Workers option: Workers can host the React dashboard and proxy
> `/api/*` + `/v1/*` to this HTTPS backend, but cannot run the Python FastAPI
> backend, PostgreSQL, or Redis themselves. See [`CLOUDFLARE.md`](CLOUDFLARE.md).

## 1. Prerequisites

- A host with Docker Engine + compose v2.
- A DNS name you control, e.g. `routellm.example.com`, with an `A`/`AAAA` record
  pointing at that host.
- Inbound ports `80/tcp` and `443/tcp` open. Caddy uses HTTP-01/TLS-ALPN on 80/443
  to obtain and renew Let's Encrypt certificates.
- A Fernet encryption key for BYOK provider secrets:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 2. Configure and launch

```bash
cp .env.example .env
```

Edit `.env`:

```env
DOMAIN=routellm.example.com
PROVIDER_KEY_ENCRYPTION_KEY=<paste the generated Fernet key>

# Optional operator/demo fallback keys. Per-caller BYOK keys override these.
GROQ_API_KEY=
GEMINI_API_KEY=
DEEPSEEK_API_KEY=

POSTGRES_PASSWORD=<choose a strong password>
```

Then:

```bash
docker compose up --build -d
docker compose logs -f caddy
```

Caddy terminates TLS and routes:

| Public URL | Upstream |
| --- | --- |
| `https://DOMAIN/` | frontend nginx (React SPA) |
| `https://DOMAIN/api/v1/...` | backend FastAPI |
| `https://DOMAIN/v1/...` | backend FastAPI |
| `https://DOMAIN/docs` | backend Swagger UI |
| `https://DOMAIN/health` | backend health |

For local experiments, leave `DOMAIN=localhost`. Caddy will use its internal CA
instead of Let's Encrypt; browsers/curl may need to trust that CA or use `-k` for
quick local checks. Do not use `localhost` mode for a public webhook endpoint.

## 3. Generate a RouteLLM caller key

```bash
curl -sS -X POST "https://routellm.example.com/api/v1/keys/generate" \
  -H "Content-Type: application/json" \
  -d '{"label":"production webhook"}'
```

The response contains `api_key` exactly once. Store it in your app's secret
manager/environment, not in frontend code.

## 4. Store provider keys for that caller (BYOK)

```bash
RK="rk_..."

curl -sS -X PUT "https://routellm.example.com/api/v1/provider-keys/groq" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $RK" \
  -d '{"api_key":"gsk_your_groq_key"}'

curl -sS "https://routellm.example.com/api/v1/provider-keys" \
  -H "X-API-Key: $RK"
```

Provider keys are encrypted at rest with `PROVIDER_KEY_ENCRYPTION_KEY` and scoped
to the calling `rk_` key. Once at least one provider key is stored for a caller,
automatic routing and fallback stay within that caller's configured providers.
Callers with no stored provider keys keep the legacy operator/demo behavior.

## 5. Call RouteLLM from a webhook

### curl

```bash
curl -sS -X POST "https://routellm.example.com/api/v1/route" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $RK" \
  -d '{"prompt":"Summarize this support ticket: ...","task_type":"summarization"}'
```

### Browser/server JavaScript (fetch)

Run this on your server or in a serverless function. Do not embed `rk_` secrets in
public browser bundles.

```js
const response = await fetch("https://routellm.example.com/api/v1/route", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-API-Key": process.env.ROUTELLM_API_KEY,
  },
  body: JSON.stringify({ prompt: "Summarize this support ticket: ..." }),
});

if (!response.ok) throw new Error(`RouteLLM HTTP ${response.status}`);
const data = await response.json();
console.log(data.model_used, data.result);
```

### OpenAI-compatible endpoint

```bash
curl -sS -X POST "https://routellm.example.com/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $RK" \
  -d '{
    "model": "routellm-auto",
    "messages": [{"role":"user","content":"Hello"}]
  }'
```

With the OpenAI Python SDK:

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://routellm.example.com/v1",
    api_key="unused",  # RouteLLM authenticates via X-API-Key, not Authorization
    default_headers={"X-API-Key": "rk_..."},
)
resp = client.chat.completions.create(
    model="routellm-auto",
    messages=[{"role": "user", "content": "Hello"}],
)
print(resp.choices[0].message.content)
```

## 6. Example webhook receiver

`examples/webhook/receiver.py` is a minimal FastAPI webhook that verifies a shared
secret, calls RouteLLM over HTTPS, and returns the routed answer. Run it with:

```bash
pip install fastapi uvicorn httpx
export ROUTELLM_BASE_URL=https://routellm.example.com
export ROUTELLM_API_KEY=rk_...
export WEBHOOK_SECRET=choose-a-long-random-secret
uvicorn examples.webhook.receiver:app --host 0.0.0.0 --port 9000
```

Then call your receiver:

```bash
curl -sS -X POST "http://localhost:9000/webhook" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
  -d '{"text":"Summarize: the customer cannot reset their password."}'
```

In production, put the receiver behind HTTPS too, verify the upstream platform's
signature scheme (HMAC, etc.), and keep `ROUTELLM_API_KEY` server-side.

## Security notes

- `rk_` caller keys are stored as SHA-256 hashes only.
- Provider keys are stored as Fernet ciphertext plus a last-4 hint; raw provider
  keys are never returned after saving.
- `PROVIDER_KEY_ENCRYPTION_KEY` must be kept secret and stable. Losing it makes
  stored provider keys undecryptable; callers must re-enter them.
- This is still lightweight demo/portfolio auth, not a full account system.
  For a public multi-tenant service, add real account auth, per-tenant rate
  limits, audit logging, and a managed secrets/KMS layer.
