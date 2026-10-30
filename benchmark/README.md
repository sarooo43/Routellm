# RouteLLM Benchmark — naive baseline vs full routing system

Concrete numbers instead of vague resilience claims: the same prompt set is
sent through (a) a naively hardcoded single model with **zero fallback**, and
(b) RouteLLM's full classify → rank → fallback pipeline, under **identical**
conditions — including a deterministic, chaos-injected provider outage.

## Layout

```
benchmark/
├── test_prompts.json        # 37 prompts: 10 code, 7 reasoning, 7 summarization, 7 qa, 6 general
├── run_benchmark.py         # the runner (one invocation per scenario)
├── generate_report.py       # REPORT.md + 2 charts from the scenario files
└── results/                 # scenario JSON, REPORT.md, *.png
```

## Why `force_provider` = gemini for the naive baseline?

The naive baseline answers "what if we had just hardcoded ONE model with no
resilience system at all". We deliberately pick **gemini**:

- It is mid-pack everywhere — it is never the runaway best choice the scorer
  would pick anyway (groq wins `qa`, deepseek wins `code`/`reasoning`), so the
  comparison is not rigged in RouteLLM's favor on capability.
- It is a real first-guess many teams would hardcode: a well-known generalist.
- When chaos injects an outage **on gemini specifically**, the naive baseline
  thereby simulates "the model everyone hardcoded just went down mid-usage",
  which is exactly the failure mode fallback exists for.

Override with `--baseline-provider <name>` if you want to argue with a
different model — the comparison code does not care.

## Prerequisites

1. A **running backend** at `http://localhost:8000` (the script never starts
   one itself):

   ```bash
   # from backend/ — with backend/.env fully configured (DB + Redis + keys)
   uvicorn app.main:app --reload
   # or, from the repo root:
   docker compose up
   ```

2. Dependencies — reuse the backend venv (it already has httpx and pydantic_settings):

   ```bash
   # from backend/ (adjust for Windows: .venv\Scripts\python.exe)
   python -m pip install matplotlib    # only generate_report.py needs it
   ```

3. **Disable the rate limit for the benchmark run.** The script fires
   `37 requests × 2 arms × 2 scenarios = 148` calls at `POST /api/v1/route`.
   The default `ROUTE_RATE_LIMIT=30/minute` would trip after ~31 calls and
   poison the sample (the script aborts loudly with a 429 — it refuses to
   publish rate-limited numbers). In `backend/.env` set:

   ```env
   ROUTE_RATE_LIMIT=100000/minute
   ```

## Running

Run **two** invocations — one per scenario — then generate the report
(all from `benchmark/`, with the backend venv's python):

```bash
# Scenario 1 — normal conditions, both arms on healthy providers
python run_benchmark.py --scenario normal

# Scenario 2 — deterministic outage: chaos injected on gemini before both
# arms run, cleared automatically after (backend must run with
# CHAOS_MODE_ENABLED=true in backend/.env, else the script aborts with a
# clear message telling you to enable it)
python run_benchmark.py --scenario chaos

# Build the tables + charts
python generate_report.py
```

Outputs land in `results/`:

| File | Contents |
|---|---|
| `scenario_1_results.json` | normal conditions — both arms' aggregates + per-prompt rows |
| `scenario_2_results.json` | chaos outage — both arms' aggregates + per-prompt rows |
| `REPORT.md` | markdown summary + per-task-type breakdown |
| `success_rate_comparison.png` | 4-bar success-rate chart (README embed) |
| `cost_comparison.png` | 4-bar cost chart (README embed) |

## What the runner guarantees

- **Fail-loud sanity gates**: unreachable backend, malformed
  `test_prompts.json`, chaos disabled during the chaos scenario, or a
  tripped rate limiter all abort the run with explicit instructions — no
  half-trustworthy data is ever written.
- **Per-prompt resilience**: an individual request that throws mid-run
  (network blip) is recorded as a failure and the run continues.
- **Deterministic adversarial conditions**: the chaos scenario uses the same
  Phase 10 chaos endpoints the dashboard demo does, so both arms experience
  the identical outage instead of random real-world failures.
- **One source of truth for pricing**: cost is computed with the same
  `COST_PER_1K_TOKENS` table imported directly from `backend/app/core/scorer.py`.

## Reading the results

Expected shape of the story (fill in real numbers after running):

- Normal conditions: both approaches succeed at similar rates — the naive
  system looks fine when nothing is broken.
- Chaos outage: the naive baseline (hardcoded gemini) fails at ~1 minus its
  success rate; RouteLLM reroutes around the outage at near-normal success.
  The **gap between those two numbers under outage** is the concrete,
  measurable value of the fallback system.
