# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

> **Name:** Routtener (previously RouteLLM Gateway) is an independent project.
> It is not affiliated with LMSYS RouteLLM, the routing-model research project
> whose name this project previously shared.

---

## [Unreleased]

### Changed

- **`ROUTELLM_EVAL_API_KEY` is renamed `ROUTTENER_EVAL_API_KEY`** in the
  resilience eval, the printed hints and the README. The old name still works
  as a fallback and the new name wins when both are set, so an exported shell
  variable or a CI secret keeps working.
- **The launcher's Codespaces hint prints a URL you can open.** It used to
  print `https://<codespace>-.app.github.dev` — the port was parsed out of a
  URL ending in `/` and then dropped. It now prints
  `https://<codespace>-<port>.<forwarding domain>`, matching what
  `_codespace_host_allowed` already accepted.
- **"No provider key configured yet" also counts keys saved in the database.**
  The banner looked only at the environment and `.env`, so it claimed no key
  was configured while the dashboard listed three. It reads `provider_keys`
  read-only and still decrypts nothing.
- **README and launcher say Node 22.19+, not 20+.** vite 8, vitest 5 and jsdom
  30 (which pulls in undici 8, needing zstd in `node:zlib`) all refuse Node 20.
  `frontend/package.json` now declares that floor in `engines`, which the CI
  comment already pointed at, and `run.py` compares major *and* minor.
- **LICENSE copyright holder is `Saboor Nasar`** (2026, the first commit year).
  The licence remains MIT.
- **The "different HTTP stacks" explanation for the discovery-vs-chat split is
  retracted.** Both paths use httpx and build `Authorization: Bearer {key}`
  identically, so no stored value can make one send and the other fail
  locally. What is real: discovery omits the header for an empty key while
  chat sends `Bearer `. The owner's specific split stays unexplained rather
  than explained by a hypothesis the code contradicts.

- **Renamed the project RouteLLM → Routtener.** Display name, dashboard title
  and header, OpenAPI title, launcher banner, health `service` field, release
  archive name, and docs. Compatibility surfaces deliberately unchanged so
  existing installs keep working:
  - the `routellm.db` SQLite filename, `encryption.key`, the `./data`
    directory, all DB tables/columns, and the OS user-data directory name —
    a pre-rename data directory decrypts and serves unchanged;
  - `ROUTELLM_*` environment variables keep working as a documented fallback;
    the new primary prefix is `ROUTTENER_*` (e.g. `ROUTTENER_DATA_DIR`,
    `ROUTTENER_CAPTURE_PROVIDER_SHAPES`), and the new one wins if both are set;
  - gateway API keys: the `rk_` key prefix, generation and the hash-based
    authentication path are unchanged, so every existing key still
    authenticates;
  - the OpenAI-compatible `routellm-auto` model id, the
    `x_routellm_metadata` response field, the `x-routellm-source` Worker
    proxy header, and the browser localStorage keys.
- Cloud CI jobs (`hf-space-boot`, `cloudflare-worker`) and `docs/deploy`
  contents are intentionally left as they were on main.

- CI now tests the launcher's full support range: the backend suite runs on
  Python 3.11 and 3.12, and the frontend job type-checks, lints and builds, so a
  broken `frontend/dist` cannot reach a user's first run.
- **CI and repo trimmed of the cloud experiments.** The `cloudflare-worker` and
  `hf-space-boot` CI jobs are removed (the remaining jobs: secret-scan,
  backend tests on 3.11+3.12, the slow git-history suite, an informational
  Python 3.13 run, and the frontend job). The Worker code is archived — not
  deleted — under `docs/deploy/cloudflare/` with the exact state of the
  attempt tagged `cloud-deploy-attempt`; the root `wrangler` devDependency and
  the `deploy` / `test:cloudflare` scripts remain in the repository. Local-first
  (`python run.py`) is the supported path.
- The dashboard's unreachable-backend copy now tells a local user to start the
  app instead of blaming a worker proxy; the Providers page uses the error
  taxonomy instead of answering "Is the backend running?" for every failure.
- `PORT` defaults to 8000 and `HOST` to `127.0.0.1`.
- `/health` reports the effective database driver (including the SQLite
  default) and stays a 200 with component state, never a 503.
- Import-time logging was removed; startup emits one line per subsystem.


### Added

- Prompt classification into `code`, `reasoning`, `summarization`, `qa` and
  `general`, then capability/cost/latency/availability scoring and ranking.
- Execution with automatic fallback, per-provider circuit breaker (closed →
  open → half-open), daily quota pre-flight gates, and a recorded attempt chain.
- Provider adapters for Groq, Google Gemini and DeepSeek behind one
  `ModelProvider` interface returning a structured `ProviderResponse`.
- FastAPI gateway: `POST /api/v1/route`, `/models`, `/logs`, `/analytics`,
  `/health`, a static model catalog, and OpenAI-compatible
  `POST /v1/chat/completions` plus `GET /v1/models`.
- Bring-your-own-key provider storage, encrypted at rest with Fernet and scoped
  per RouteLLM key, with hint-only disclosure.
- React + TypeScript dashboard (dark glassmorphism): overview KPIs, live feed,
  model health, routing explorer, analytics, catalog, API keys, provider keys.
- Error taxonomy shared by the API and the UI (network, unauthorized, rate
  limited, service unavailable, validation, server error) with actionable copy.
- Chaos-mode fault injection for demonstrating failover.
- **Live-eval tests run the real script as a subprocess** against a real
  uvicorn on an OS-chosen port: 0/1/2 providers saved, Chaos disabled, a minted
  key provably revoked, a user-supplied key never revoked, and zero
  `/api/v1/route` calls on `--dry-run`.
- **Provider wire-contract tests over a real local socket**, replacing the
  assumption that a `respx` fake returns the shape a provider really sends.
- **The benchmark report also lands in `docs/benchmark-results.md`.** The one
  benchmark command now writes its Markdown report to
  `benchmark/results/REPORT.md` AND the repo-root docs location the README
  links to. Both are produced by your own run; no benchmark numbers are
  committed to the repository.
- **One-command local run.** `python run.py` (also `./start.sh`,
  `.\start.ps1`) creates a virtualenv, installs dependencies, builds the
  dashboard or uses a prebuilt one, creates the data directory, and serves the
  app at `http://127.0.0.1:8000`. `--dev` runs uvicorn reload plus Vite.
- **Single-URL serving.** The backend serves the built dashboard, with SPA
  fallback for client-side routes mounted after the API, cache headers for
  hashed assets, and an explanatory page when no build exists.
- **SQLite by default.** `DATABASE_URL` unset means
  `sqlite+aiosqlite:///<data dir>/routellm.db`; Postgres still works when it is
  set. Tables are created automatically on first run.
- **In-process store instead of Redis.** `REDIS_URL` unset uses an in-process
  Redis-compatible client, covering the circuit breaker, quota tracker, chaos
  controller and health probe.
- **A generated BYOK encryption key.** Written once to
  `<data dir>/encryption.key` (`0600` where supported), reused after that, and
  never logged or returned.
- **No key required locally.** `REQUIRE_API_KEY=false` (default) attributes
  requests to a stable local identity row, so per-caller provider keys and
  usage counters keep working. `true` retains the previous `X-API-Key` contract.
- **Demo mode.** A labelled `mock` provider with simulated latency and an
  optional failure rate, reachable only when a request names it. It never
  participates in automatic routing, the catalog, model health, BYOK or the
  analytics legend, so synthetic traffic cannot reach a real measurement.
- **First-run onboarding.** `GET /api/v1/setup/status` reports which providers
  have keys (never the keys), free-key links, and the remaining steps. The
  checklist is dismissible, persisted and self-checking; a `role="status"`
  banner covers the no-key case.
- **Chaos mode on by default** for local runs, with per-request route gating so
  `CHAOS_MODE_ENABLED=false` still answers a native 404 and hides the endpoints
  from `/docs`.
- **Packaging.** A single-service Docker image and `docker-compose.yml` (API +
  built dashboard, `/data` volume, non-root), plus a release workflow on `v*`
  tags that zips the backend with a prebuilt `frontend/dist`, smoke-tests the
  archive, and attaches it.
- **Stale-build detection.** `/health` (and `/api/v1/health`) now report the
  backend's git commit and commit time (`build.git_commit` /
  `build.git_commit_time`, fail-soft where git is absent); the dashboard is
  stamped with the commit its bundle was built from and shows both in the
  footer. When the two commits differ, a banner names both and says to
  restart `python run.py` — no more debugging an old bundle as if the app
  were broken.
- **Stale-dashboard rebuild.** `python run.py` now rebuilds the dashboard when
  `frontend/` sources are newer than `dist/`, so an outdated bundle cannot
  survive a restart just because a build exists.
- **A benchmark the owner can run.** One command
  (`backend/.venv/Scripts/python.exe benchmark/run_benchmark.py`) runs the
  naive-single-model baseline and the full router over the same 37 prompts
  under normal conditions AND a Chaos-Mode simulated outage, then writes
  `benchmark/results/REPORT.md` — a Markdown results table carrying the date,
  the models each arm used, and the naive vs router success rates. The repo
  ships no benchmark numbers: results are produced only by the owner's own
  run with their own provider keys.
- `APP_NAME` / `APP_VERSION` constants (backend `app/identity.py`, frontend
  `src/appInfo.ts`) with the LMSYS name-collision disclaimer written once.
- MIT licence, CONTRIBUTING, SECURITY (with a local-first threat model),
  CODE_OF_CONDUCT, issue and PR templates, and this changelog.
- Tests: executor, scoring, breaker, quota, auth matrix, OpenAI compatibility,
  deployment resilience, dashboard components, local runtime contracts, a
  Redis contract suite run against the in-process store and real Redis when
  configured, demo-mode honesty, launcher behaviour, and packaging invariants.

### Fixed

- **A failed provider call no longer shows a raw exception class.** A route
  that could not reach a provider used to answer
  `unexpected_error: ConnectError`, which starts people debugging the gateway
  when the gateway is fine. Responses now carry a diagnosis that names the
  provider, says what actually failed and what to do next
  (`network_unreachable: this machine could not open a connection to groq …`).
  Covers missing keys, rejected keys, provider outages, rate limits and chaos
  injections, on `/api/v1/route` and `/v1/chat/completions`. The stored log
  keeps the raw code for debugging.
- **The dashboard no longer claims the backend has stopped.** The
  unreachable-state title is now "No response from the backend" with retry and
  start instructions, instead of asserting the process is dead.
- **The connectivity pill re-probes when a request fails**, so the header cannot
  read "Online" while the page underneath is showing an error.
- **Validation errors show the server's sentence.** A 422 from the provider-key
  form used to be flattened to "Request failed"; it now says e.g. "Provider key
  must be between 8 and 512 characters."
- **Tests: `cleanup()` was never registered.** Rendered trees leaked between
  tests, so queries matched several elements and files were ~5x slower.

### Deprecated

- The Hugging Face Space deployment path is documented as experimental under
  `docs/deploy/`. Local-first is the supported path.
- The Cloudflare Worker deployment attempt is **closed**: the code is archived
  under `docs/deploy/cloudflare/` and tagged `cloud-deploy-attempt`. It is
  kept for reference only and is not tested by CI anymore.

---

[Unreleased]: https://github.com/sarooo43/Routtener/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sarooo43/Routtener/releases/tag/v0.1.0
