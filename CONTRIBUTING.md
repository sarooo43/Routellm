# Contributing to RouteLLM Gateway

Thanks for taking a look. This is a small, deliberately readable project: a
FastAPI gateway that classifies a prompt, ranks providers, executes with
automatic fallback, and shows what happened — plus a React dashboard.

Everything below can be run on a laptop with no Docker, no Postgres and no
Redis.

> **Name note:** this project is not affiliated with LMSYS RouteLLM, the
> routing-model research project of the same name.

## The 60-second setup

```bash
git clone https://github.com/sarooo43/Routellm.git
cd Routellm
python run.py
```

`run.py` creates `.venv`, installs dependencies, builds the dashboard if Node
is available, creates `./data` and `backend/.env` on first run, and serves
`http://127.0.0.1:8000`.

For backend work with reload plus the Vite dev server:

```bash
python run.py --dev
# backend  http://127.0.0.1:8000  (auto-reload)
# frontend http://localhost:5173  (proxies /api, /v1, /health to the backend)
```

Useful flags: `--port 8123`, `--no-browser`, `--reinstall`, `--rebuild`,
`--check` (verify the environment and exit).

## Layout

```
run.py                 one-command launcher (stdlib only)
backend/app/           FastAPI app: api/, core/, providers/, db/, models/
backend/tests/         unit + integration tests (SQLite in memory)
frontend/src/          React dashboard (Vite, TypeScript, Tailwind)
cloudflare/            optional Worker proxy (experimental)
docs/                  deployment notes for the optional cloud path
```

## Tests

Run the backend suite from `backend/`:

```bash
cd backend
../.venv/bin/python -m pytest              # everything
../.venv/bin/python -m pytest -q tests/integration/test_demo_mode.py
```

To run the suite the way CI does (Postgres and a real Redis instead of the
local defaults). This is what catches environment-dependent tests, and it is
how the failures in `test_demo_mode.py` and `test_redis_contract.py` were
found:

```bash
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/routellm_test \
REDIS_URL=redis://localhost:6379/0 \
  ../.venv/bin/python -m pytest
```

CI runs that matrix on Python 3.11 and 3.12; the launcher supports 3.11+, so a
change that needs a newer interpreter should say so.

The frontend:

```bash
cd frontend
npm test          # vitest
npx tsc -b        # type check
npx oxlint src    # lint
npm run build     # production build
```

Secrets before you push:

```bash
python backend/scripts/check_secrets.py     # tree + full history, masked output
```

Please add a test with a behavioural change. This project's tests are its
documentation: they spell out what a 503 means, why the demo provider is never
auto-selected, and what a missing encryption key does.

## Ground rules

These are enforced by the test suite, not just by review:

- **Never commit a secret.** Provider keys, `PROVIDER_KEY_ENCRYPTION_KEY`,
  database URLs with passwords, `rk_` keys and tokens stay out of git — in
  commits, tests, fixtures, logs and screenshots. `backend/.env` is ignored;
  `backend/.env.example` is the only tracked env file. See
  [SECURITY.md](SECURITY.md).
- **Async everywhere in the backend.** No blocking calls in a request path.
- **No hardcoded credentials** in code or tests. Mock the provider; never call
  a paid API from a test.
- **Structured responses.** Providers return a `ProviderResponse`; they must
  never raise into the executor.
- **Fail soft, say why.** An unreachable dependency is a degraded state with a
  clear message, never a crash and never a generic "it's broken".
- **No invented numbers.** Benchmarks and README performance claims must come
  from a real run you can reproduce. Placeholders stay placeholders.
- **Demo traffic is labelled.** Anything synthetic is marked `MOCK` and
  excluded from measurements.

## Style

- Backend: type hints on public functions, docstrings that explain *why* a
  decision was made (the *what* is the code), `ruff`-friendly formatting.
- Frontend: TypeScript strict mode, function components, Tailwind classes,
  accessible names on interactive elements (`aria-label`, `role="status"` vs
  `role="alert"` matters here — a missing key is not an error).
- Commits: small and logical, imperative subject line, body explaining the
  reasoning when it is not obvious.

## Pull requests

1. Branch from `main`.
2. Keep the diff focused; unrelated cleanups belong in their own PR.
3. Make sure backend tests, frontend tests, `tsc` and lint pass.
4. Describe what you verified and how. "Should work" is not verification.
5. Windows-only failures should be marked `skipif(sys.platform == "win32")`
   with a reason, not silently ignored.

## Reporting bugs

Include: what you ran, what happened, what you expected, and your OS/Python
(or Node) version. If the dashboard is involved, the browser console and the
`/health` response are usually enough to identify the cause.

**Do not paste API keys or a `.env` file into an issue.** Mask anything that
looks like a credential.

## Licence

Contributions are accepted under the [MIT Licence](LICENSE).
