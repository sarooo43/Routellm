# RouteLLM Gateway

**An LLM router you can actually run.** Point one endpoint at several providers;
it classifies each prompt, ranks Groq / Google AI Studio / DeepSeek, and falls
back automatically when one is down, rate-limited or circuit-broken — while
showing you exactly what it decided and why.

[![CI](https://github.com/sarooo43/Routellm/actions/workflows/ci.yml/badge.svg)](https://github.com/sarooo43/Routellm/actions/workflows/ci.yml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab.svg)](https://www.python.org/downloads/)
[![Node 20+ (only to build the UI)](https://img.shields.io/badge/node-20%2B%20(optional)-339933.svg)](https://nodejs.org)

> **Name:** this project is independent and **not affiliated with LMSYS RouteLLM**, the routing-model research project of the same name.

<!--
  Demo placeholder — replace with a real screen recording before announcing.
  Nothing below claims a measured result yet; see "Benchmarks".
-->

<!-- OWNER TASK: record docs/assets/demo.gif (see docs/assets/README.md).
     Kept as a placeholder line rather than a broken image. -->
> _Demo GIF: to be recorded. `docs/assets/README.md` lists exactly what to film._

---

## 60-second quick start

No Docker. No Postgres. No Redis. No account.

```bash
git clone https://github.com/sarooo43/Routellm.git
cd Routellm
python run.py
```

`run.py` creates a virtualenv, installs the backend, builds the dashboard if
Node is available (or uses a prebuilt one), creates `./data`, and opens
**http://127.0.0.1:8000**.

<details>
<summary><strong>Windows</strong> (PowerShell)</summary>

```powershell
git clone https://github.com/sarooo43/Routellm.git
cd Routellm
.\start.ps1
```

If PowerShell blocks local scripts:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

`start.ps1` only locates Python and hands over to `run.py`; the
`ExecutionPolicy Bypass` form scopes the change to that one invocation, so
nothing needs to be made permanent.

</details>

<details>
<summary><strong>macOS / Linux</strong></summary>

```bash
git clone https://github.com/sarooo43/Routellm.git
cd Routellm
./start.sh
```

Python 3.11+ is the only hard requirement. `--check` reports what is present
and what would be installed, without changing anything:

```bash
python run.py --check
```

</details>

<details>
<summary><strong>No Node.js installed?</strong></summary>

The dashboard needs a build. Without Node, the launcher runs the API only and
`/` explains how to build the UI. Two ways to get the UI anyway:

- install Node 20+ and rerun `python run.py`, or
- download a release zip — it ships a prebuilt `frontend/dist`, so
  `python run.py` serves the full app with no Node at all.

</details>

### Then, in the app

1. **Providers** → paste a free key (Groq, Google AI Studio or DeepSeek — links
   are on the page). Keys are encrypted at rest and win over environment
   variables.
2. **Explorer** → send a prompt and watch the routing decision.
3. Leave it alone for demo mode: with no key configured, a **labelled MOCK**
   provider answers so the UI is explorable without spending anything.

### Use it from code

It speaks the OpenAI chat-completions API, so the official SDK works unchanged:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="not-needed-locally")

reply = client.chat.completions.create(
    model="routellm-auto",          # or "groq" / "gemini" / "deepseek"
    messages=[{"role": "user", "content": "Explain circuit breakers in one line."}],
)
print(reply.choices[0].message.content)
```

`api_key` is ignored when `REQUIRE_API_KEY=false` (the local default). Set it to
`true` to require a generated `rk_…` key.

---

## What it does

**Classify → rank → execute → record.** Each request is classified into `code`,
`reasoning`, `summarization`, `qa` or `general`; every candidate provider is
scored on capability, recent success rate, cost, latency and breaker
availability; the best one is called. If it fails, the next is tried, and the
whole attempt chain is persisted and shown.

```mermaid
flowchart LR
    C[Client] -->|POST /api/v1/route<br/>or /v1/chat/completions| G[FastAPI gateway]
    G --> CL[Classifier]
    CL --> R[Ranker<br/>capability · success · cost<br/>latency · availability]
    R --> X{Executor}
    X -->|1st choice| P1[Groq]
    X -->|fallback| P2[Google AI Studio]
    X -->|fallback| P3[DeepSeek]
    X -.->|no key configured| D[MOCK demo provider]
    P1 & P2 & P3 --> B[(Circuit breaker<br/>+ quota gates)]
    B --> L[(Task log<br/>SQLite or Postgres)]
    L --> U[React dashboard]
    X --> O[OpenAI-compatible response]
```

### Features

- **Automatic fallback** across providers, with the attempt chain recorded
  (provider, error class, latency, whether it was skipped or failed).
- **Circuit breaker** per provider: closed → open → half-open, so a dead
  provider is skipped instead of retried forever.
- **Quota pre-flight**: a provider close to its free-tier daily limit is
  skipped *before* burning a request on a guaranteed 429.
- **Live dashboard** (dark glassmorphism): KPIs, live feed, per-provider health,
  routing explorer, analytics, catalog and key management.
- **Bring your own key**, encrypted at rest, scoped per identity, never echoed
  back after saving.
- **OpenAI-compatible** `/v1/chat/completions` and `/v1/models`.
- **Demo mode**: a MOCK provider for a zero-key, zero-spend first run. Labelled
  everywhere and excluded from every real number.
- **Chaos mode**: simulate a provider outage and watch the failover happen.
- **Everything optional**: SQLite or Postgres, in-process store or Redis,
  local only or containerised.

---

## Configuration

Nothing below is required for a local run. Copy
[`backend/.env.example`](backend/.env.example) to `backend/.env` to override a
default (the launcher creates it for you on first run).

| Variable | Default | What it changes |
|---|---|---|
| `ROUTELLM_DATA_DIR` | `./data` | Where SQLite, the generated encryption key and logs live |
| `DATABASE_URL` | unset → SQLite | Set any Postgres DSN to use Postgres instead |
| `REDIS_URL` | unset → in-process | Set it to share breaker/quota/chaos state across processes |
| `REQUIRE_API_KEY` | `false` | `true` requires `X-API-Key` on `/api/v1/route` and `/v1/chat/completions` |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address and port |
| `SERVE_FRONTEND` | `true` | Serve the built dashboard from the API process |
| `FRONTEND_DIST_DIR` | `frontend/dist` | Explicit path to a built dashboard |
| `CHAOS_MODE_ENABLED` | `true` | Fault-injection routes (hidden and 404 when false) |
| `DEMO_MODE_ENABLED` | `true` | Registers the `mock` demo provider |
| `DEMO_LATENCY_MS` / `DEMO_FAILURE_RATE` | `350` / `0.0` | Simulated latency and failure rate for demo mode |
| `PROVIDER_KEY_ENCRYPTION_KEY` | auto-generated | Fernet key for stored provider keys |
| `GROQ_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY` | unset | Provider keys from the environment (the Providers page wins) |
| `ENABLE_ML_ROUTING` | `false` | Experimental ML-assisted ranking (needs `requirements-ml.txt`) |
| `CORS_ORIGINS` | unset | Comma-separated origins, only for cross-origin clients |

`python run.py --help` lists the launcher flags: `--port`, `--host` (prints a
warning off loopback), `--dev`, `--no-browser`, `--reinstall`, `--rebuild`,
`--check`.

### Where your data lives

`./data` (git-ignored) holds `routellm.db`, `encryption.key` and logs. It is
never uploaded anywhere. Delete it to start clean — **deleting
`encryption.key` makes previously stored provider keys unreadable**, so
re-enter them on the Providers page.

---

## Benchmarks

**Placeholder — no numbers are claimed yet.**

The repository contains a benchmark harness (`benchmark/`) that compares a
naive single-model baseline against this router under identical conditions,
including a deterministic simulated outage. A real run needs real provider keys
and must be regenerated before anything is published here:

```bash
cd benchmark
pip install -r requirements.txt
python run_benchmark.py --scenario normal
python run_benchmark.py --scenario outage
python generate_report.py
```

The currently committed `benchmark/results/REPORT.md` is from a run **without**
provider credentials (every arm reports 0% success). It is kept only as an
artefact of the harness and must not be quoted as a result. Demo-mode traffic is
never included in benchmark runs.

---

## Privacy

Local-first is the point, not a setting.

- **Your prompts go to the provider you configured** — Groq, Google AI Studio
  or DeepSeek — and nowhere else. There is no telemetry, no analytics beacon and
  no phone-home.
- **Demo mode makes no network call at all.**
- **Provider keys never leave the machine.** They are Fernet-encrypted before
  touching the database and returned only as a 4-character hint.
- **The gateway binds `127.0.0.1`.** `--host 0.0.0.0` prints a warning, because
  anyone who can reach the port can spend your provider credits.
- **Never commit `backend/.env`.** `backend/scripts/check_secrets.py` scans the
  working tree *and* git history for credentials and masks anything it finds.

Full threat model: [SECURITY.md](SECURITY.md).

---

## Optional: Postgres, Redis, containers, ML

None of these are needed for local use.

**Postgres / Redis** — point the app at them; nothing else changes:

```bash
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/routellm
REDIS_URL=redis://localhost:6379/0
```

The Redis-facing consumers (circuit breaker, quota, chaos, health) are tested
against both the in-process store and a real Redis with the same assertions
(`tests/integration/test_redis_contract.py`).

**Container** — one service, dashboard included:

```bash
docker compose up --build      # http://localhost:8000
```

**ML-assisted routing** (experimental, off by default):

```bash
pip install -r backend/requirements-ml.txt
# then set ENABLE_ML_ROUTING=true once a model exists
```

**Cloud** — the previous Hugging Face Space / Cloudflare Worker path still
works and is still tested, but it is documented as experimental in
[docs/deploy/](docs/deploy/) and is not the recommended way to run this.

---

## Scope decisions

Being explicit about what this is and is not:

- **Local-first, single user.** Accounts, teams and hosted billing are out of
  scope. `REQUIRE_API_KEY` gives you a shared key, not a user system.
- **Three providers, one interface.** Groq, Google AI Studio and DeepSeek ship
  today; adding a fourth means implementing `ModelProvider` and registering it.
- **Rules before models.** Routing is a transparent, inspectable scoring table.
  ML ranking exists behind a flag and is not trusted by default.
- **SQLite over migrations.** Tables are created from the models on first run.
  For a long-lived deployment with schema changes, add Alembic.
- **In-process state by default.** Breaker/quota/chaos state resets on restart
  unless you set `REDIS_URL`. That is the right trade for one machine.
- **No fabricated numbers.** Anything claimed in this README is either
  reproducible from the repository or marked as a placeholder.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `python run.py` says *Python 3.11+ is required* | Install a newer Python, or on Debian/Ubuntu `sudo apt install python3.12 python3.12-venv` |
| Launcher reports *Port 8000 is busy* | It auto-advances and prints the port it used; pass `--port` to choose one |
| Dashboard at `/` shows a status page instead of the app | `frontend/dist` is missing — run `npm ci && npm run build` in `frontend/`, or `python run.py --rebuild` |
| Pages load but every number is empty | Backend not reachable: check `http://127.0.0.1:8000/health`. The pill in the top bar says which dependency is down |
| `/health` reports `"db": "down"` | The SQLite file is missing or unwritable — check `ROUTELLM_DATA_DIR`, or your `DATABASE_URL` if you set one |
| Saving a provider key returns 503 | No usable encryption key: make the data directory writable (a key is generated automatically), or set `PROVIDER_KEY_ENCRYPTION_KEY` |
| Stored provider keys stop working after you deleted `data/` | The generated key is gone, so the ciphertext cannot be decrypted — re-enter the keys |
| Every route fails with a connection error | No provider key is configured and demo mode is off, or this machine cannot reach the provider API |
| Providers page says the key is fine but routes fail | Fixed: the badge used to mean "a row exists". Each provider now has a **Test connection** button that makes the same call routing makes and reports the real verdict — `valid`, `auth_failed`, `model_not_found`, `rate_limited`, `network_error` — plus the model it used |
| `model_not_found: … has no model named 'x'` | The key is fine; the model is not. The app discovers models per key, so this means a pinned override (`GROQ_MODEL` / `GEMINI_MODEL` / `DEEPSEEK_MODEL`) points at a retired model — clear it, or pick one from the Providers-page dropdown |
| `auth_failed: … rejected the credential (HTTP 401)` | The only error that means "check your key". Re-copy it from the provider console; nothing else in this list blames the key |
| `network_error: could not open a connection` | This machine cannot reach the provider (proxy, firewall, DNS). Not a key problem |
| Want to check every provider from a terminal | `python backend/scripts/check_providers.py` — reads keys from the environment or `.env` (never from flags), prints the key masked to its last 4 characters, the first chat models each key can use, the model the app would choose, and the result of a real 1-token request. Exit code 0 means every configured provider works |
| `.\start.ps1` is blocked by PowerShell | `powershell -ExecutionPolicy Bypass -File .\start.ps1` |

Logs are JSON on stdout; set `LOG_LEVEL=DEBUG` for more. Secrets are redacted
before anything is written, and provider error bodies are kept to ~200 sanitized
characters so you can see *what the provider actually said* without leaking a key.

### Which providers and models are used

Models are **discovered, not hardcoded**: on the first request for a provider the
gateway asks that provider's list-models endpoint with your key, keeps the
chat-capable ids, and picks the first one available from an ordered preference
list. The result is cached for an hour (in memory, and in the database so a
restart does not re-discover) and refreshed automatically if a model disappears.

To pin a model instead, set `GROQ_MODEL`, `GEMINI_MODEL` or `DEEPSEEK_MODEL`, or
choose one in the Providers page. Endpoints are fixed and tested verbatim:
Groq `https://api.groq.com/openai/v1`, Gemini `v1beta …/models/<model>:generateContent`,
DeepSeek `https://api.deepseek.com/v1`.

---

## Roadmap

- [ ] Real benchmark run with real keys, and a published report
- [ ] Demo GIF and screenshots in this README
- [ ] Alembic migrations for schema changes
- [ ] More providers behind the same interface
- [ ] Optional streaming for `/v1/chat/completions`

Completed work is tracked in [CHANGELOG.md](CHANGELOG.md).

---

## Contributing

Bug reports, provider adapters and dashboard polish are all welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md) — it covers the dev loop, the test commands
and the rules the suite enforces (no secrets, no invented numbers, demo traffic
stays labelled).

Please also read [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). For security issues,
follow [SECURITY.md](SECURITY.md) rather than opening a public issue.

---

## Licence

[MIT](LICENSE) © sarooo43.

Not affiliated with LMSYS RouteLLM.
