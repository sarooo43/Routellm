# RouteLLM Backend

RouteLLM is an intelligent LLM routing gateway that sits between client applications and multiple AI providers (Groq, Gemini, and DeepSeek to start). Instead of calling a single model endpoint, clients send requests to RouteLLM, which classifies the request, scores the available models, and executes the best one; if that model fails, is rate-limited, or produces bad output, the gateway automatically falls back to the next best model — without the calling application ever seeing an error. Think of it as a self-hosted mini version of what OpenRouter or Portkey offer commercially, focused on resilience.

## Status: work-in-progress

This project is built phase-by-phase. The current phase establishes the FastAPI skeleton: project structure, configuration via `pydantic-settings`, structured (JSON) logging with `structlog`, and a health endpoint. Later phases add the provider layer, routing core (classifier, scorer, executor, circuit breaker, quota tracker), database models, and the API routes.

## Project structure

```
backend/
├── app/
│   ├── main.py         # FastAPI app, lifespan events, CORS, /health
│   ├── config.py       # pydantic-settings, singleton `settings`
│   ├── api/            # routers + dependencies (empty for now)
│   ├── core/           # routing engine (later phases)
│   ├── providers/      # provider adapters (later phases)
│   ├── models/         # DB models (Phase 6)
│   ├── db/             # DB session (Phase 6)
│   └── utils/          # structured logger
├── tests/
│   ├── unit/
│   └── integration/
├── requirements.txt
├── .env.example
├── pytest.ini
└── README.md
```

## Setup

### Prerequisites

- Python 3.10+

### Steps

1. Clone the repository and enter the backend folder:

   ```bash
   git clone <repo-url>
   cd RouteLLM/backend
   ```

2. Create and activate a virtual environment:

   ```powershell
   # Windows (PowerShell)
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

   ```bash
   # macOS / Linux
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Configure environment variables:

   ```powershell
   # Windows
   copy .env.example .env
   ```

   ```bash
   # macOS / Linux
   cp .env.example .env
   ```

   Then edit `.env` and fill in your API keys and service URLs (see comments in `.env.example`).
   For the per-caller BYOK Provider Keys feature, also set `PROVIDER_KEY_ENCRYPTION_KEY`:

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

5. Run the development server:

   ```bash
   uvicorn app.main:app --reload
   ```

6. Verify everything works:

   - http://localhost:8000/health → `{"status": "ok", "service": "routellm-backend"}`
   - Interactive API docs at http://localhost:8000/docs

## Running tests

```bash
pytest
```

`pytest.ini` sets `asyncio_mode = auto`, so async tests run without per-test decorators.

## BYOK and HTTPS

- Per-caller provider keys are managed at `/api/v1/provider-keys` and encrypted
  with `PROVIDER_KEY_ENCRYPTION_KEY`. See the root README and
  `docs/HTTPS_AND_WEBHOOKS.md`.
- The docker-compose stack uses Caddy for automatic public HTTPS. The backend
  container itself serves HTTP on port 8000 behind the proxy.
