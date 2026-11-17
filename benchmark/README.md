# Routtener Benchmark — naive baseline vs full routing system

Concrete numbers instead of vague resilience claims: the same prompt set is
sent through (a) a naively hardcoded single model with **zero fallback**, and
(b) Routtener's full classify → rank → fallback pipeline, under **identical**
conditions — including a deterministic, chaos-injected provider outage.

> **Every number in the results comes from your own run.** This repository
> ships **no benchmark numbers at all** — no results JSONs, no charts, no
> filled-in REPORT.md. The sandbox/CI machines that developed this code have
> no provider keys and no internet access, so they cannot produce data, and
> publishing someone else's numbers (or placeholders shaped like real ones)
> would be worse than publishing nothing. Run it once on your machine and
> `results/REPORT.md` will contain your numbers, with the date and the models
> used written right into the table.

## Layout

```
benchmark/
├── test_prompts.json        # 37 prompts: 10 code, 7 reasoning, 7 summarization, 7 qa, 6 general
├── run_benchmark.py         # the runner — ONE command runs everything
├── generate_report.py       # REPORT.md + 2 charts from the scenario files (called automatically)
└── results/                 # empty until you run it; see results/README.md
```

## The one documented command

With the backend already running (see Prerequisites), from the repo root:

```powershell
# Windows (PowerShell)
backend\.venv\Scripts\python.exe benchmark\run_benchmark.py
```

```bash
# macOS / Linux
backend/.venv/bin/python benchmark/run_benchmark.py
```

That single command:

1. validates `test_prompts.json`,
2. runs **Scenario 1 — normal conditions** (both arms on healthy providers),
3. runs **Scenario 2 — simulated outage** (Chaos Mode injects a deterministic
   outage on the baseline provider before both arms run, cleared again after),
4. writes `results/REPORT.md` — a Markdown results table with the **date**,
   the **models each arm used**, and the **naive vs router success rates**
   side by side (plus charts when matplotlib is installed).

The two scenario runs and the report can also be invoked individually:

```bash
python run_benchmark.py --scenario normal   # scenario 1 only
python run_benchmark.py --scenario chaos    # scenario 2 only
python run_benchmark.py --report            # rebuild REPORT.md, no requests sent
```

## Why `force_provider` = gemini for the naive baseline?

The naive baseline answers "what if we had just hardcoded ONE model with no
resilience system at all". We deliberately pick **gemini**:

- It is mid-pack everywhere — it is never the runaway best choice the scorer
  would pick anyway (groq wins `qa`, deepseek wins `code`/`reasoning`), so the
  comparison is not rigged in Routtener's favor on capability.
- It is a real first-guess many teams would hardcode: a well-known generalist.
- When chaos injects an outage **on gemini specifically**, the naive baseline
  thereby simulates "the model everyone hardcoded just went down mid-usage",
  which is exactly the failure mode fallback exists for.

Override with `--baseline-provider <name>` if you want to argue with a
different model — the comparison code does not care.

## Prerequisites

1. A **running backend** at `http://localhost:8000` (the script never starts
   one itself). Easiest, from the repo root:

   ```bash
   python run.py     # installs what is missing, builds the dashboard, serves
   ```

   or explicitly, from `backend/`:

   ```bash
   uvicorn app.main:app --reload
   ```

2. Dependencies — reuse the backend venv (it already has httpx). matplotlib
   is only needed for the charts; the report is written without them too:

   ```bash
   backend\.venv\Scripts\python.exe -m pip install matplotlib   # optional
   ```

3. **API keys are automatic.** The runner generates a lightweight demo key via
   `POST /api/v1/keys/generate` and sends it as `X-API-Key` on every routing
   request. No login or manual key setup is required.

4. **Real provider keys are required for real numbers.** With no keys the
   requests fail and the report will honestly show failure rows — that is
   data too, but it is not a benchmark.

5. **Disable the rate limit for the benchmark run.** The script fires
   `37 requests × 2 arms × 2 scenarios = 148` calls at `POST /api/v1/route`.
   The default `ROUTE_RATE_LIMIT=30/minute` would trip after ~31 calls and
   poison the sample (the script aborts loudly with a 429 — it refuses to
   publish rate-limited numbers). In `backend/.env` set:

   ```env
   ROUTE_RATE_LIMIT=100000/minute
   ```

6. **Chaos Mode on for the outage scenario.** In `backend/.env`:

   ```env
   CHAOS_MODE_ENABLED=true
   ```

   If it is off, the chaos scenario aborts with a clear message telling you
   exactly this (the normal scenario does not need it).

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
- **A report you can read without context**: `REPORT.md` names the generation
  date and the models each arm actually called, and states that the numbers
  come from the machine that ran the benchmark.

## Reading the results

The shape of the story to look for in YOUR report (the numbers are yours to
produce — none are printed here on purpose):

- Normal conditions: both approaches should succeed at similar rates — the
  naive system looks fine when nothing is broken.
- Chaos outage: the naive baseline (hardcoded gemini) fails at ~1 minus its
  normal success rate; Routtener reroutes around the outage at near-normal
  success. The **gap between those two numbers under outage** is the
  concrete, measurable value of the fallback system in your setup.
