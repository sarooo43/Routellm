# Is RouteLLM done? — verification runbook

Every claim below has a **command** and an **expected result**. Run the commands
in order; the project is "done" for a layer when that layer's gate passes.

Two rules for this document:

- Anything that needs a credential is marked 🔑. Nothing here ever prints a
  secret — expected outputs are shown with `****`.
- Two gates (5 and 6) can **only** be run by the owner, from outside the repo,
  because they need the live Space and the live Worker. Everything else runs
  locally or in CI.

---

## 0) The status board

| # | Layer | Gate | Status |
|---|---|---|---|
| 1 | Backend code + tests | `pytest` → 332 passed, 2 skipped | ✅ verified locally |
| 2 | Frontend build + tests | `npm run build`, `npm test` → 61 passed | ✅ verified locally |
| 3 | Cloudflare Worker | `npm run test:cloudflare` → 10 passed, `deploy:dry-run` | ✅ verified locally |
| 4 | CI | 4 jobs green on `main` | ✅ last merge `d908b79` |
| 5 | **Hugging Face Space runtime** | `/health` → `200 {"status":"ok"}` | ⬜ **owner must confirm** |
| 6 | **Cloudflare dashboard (live)** | dashboard loads, routes a request | ⬜ **owner must confirm** |
| 7 | Benchmark evidence | re-run with live keys | ⚠️ **recorded run has 0% success — see §7** |

Rows 1–4 are proven by commands reproduced in this session. Rows 5–6 need the
public URLs. Row 7 is not a code problem; it is missing data.

---

## 1) Backend — 30 seconds

```bash
cd backend
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

**Expected:** `332 passed, 2 skipped`.

The 2 skips are environmental (missing optional packages), not ignored failures:

```bash
.venv/bin/python -m pytest -q -rs | grep SKIPPED
```

**Dependency truth** — the install must match what the Space really builds,
including Gradio's `mcp` extra (this is what broke the Space twice):

```bash
.venv/bin/pip install "gradio[oauth,mcp]==6.31.0" "spaces==0.51.3"
.venv/bin/pip check                      # → No broken requirements found.
ROUTELLM_CHECK_PYPI=1 .venv/bin/python -m pytest tests/test_hf_requirements.py -q
                                         # → 8 passed
```

---

## 2) Frontend — 60 seconds

```bash
cd frontend
npm ci
npx tsc --noEmit        # → no output (clean)
npm run lint            # → Found 0 warnings and 0 errors.
npm test                # → 13 files, 61 tests passed
npm run build           # → ✓ built
```

`npm run build` prints a chunk-size warning (>500 kB). That is a performance
note, not a failure — see §8.

---

## 3) Cloudflare Worker — 30 seconds

Run from the **repo root** (the workspace root installs `wrangler`; running
`npm ci` inside `frontend/` only is why `config.test.js` can fail to import
`wrangler`):

```bash
npm ci
npm run test:cloudflare     # → # tests 10  # pass 10  # fail 0
npm run build               # builds frontend/dist, which the Worker serves
npm run deploy:dry-run      # → "Read N files from the assets directory" + bindings
```

`deploy:dry-run` is the real config validation: it parses `wrangler.jsonc`,
resolves the assets directory and prints the bindings. If it passes locally, a
Cloudflare build failure is **dashboard configuration**, not code (§8).

---

## 4) CI

```bash
gh run list --limit 1
gh run view <run-id> --json jobs --jq '.jobs[] | "\(.name): \(.conclusion)"'
```

**Expected:** `backend-tests`, `hf-space-boot`, `cloudflare-worker`,
`frontend-tests` all `success`.

`hf-space-boot` is the important one: it installs `gradio[oauth,mcp]==<sdk_version>`
plus `spaces`, `uvicorn` and `websockets` — the same closure the Space builds —
so a dependency break is caught before a deploy.

---

## 5) Hugging Face Space 🔑 — owner only

**a. Files are there.** Space → **Files**: `hf_app.py`, `requirements.txt`,
`app/` at the root. `.coverage` should be **gone** (removed in `7129777`).

**b. The runtime log shows the new code.** Space → **Logs**, look for this exact
sequence:

```text
{"event": "zerogpu_stub_registered", ...}     # only on ZeroGPU hardware
{"event": "zerogpu_report_sent", ...}         # only on ZeroGPU hardware
{"event": "hf_app_ready", "port": 7860, ...}
INFO:     Uvicorn running on http://0.0.0.0:7860
```

If instead you see `hf_app_port_unavailable`, the log now names the port and the
reason; if you see `No @spaces.GPU function detected`, the Space is running code
older than `d908b79` — redeploy.

**c. It answers over HTTPS.**

```bash
curl -s https://saboornotgonna-routellm.hf.space/health
```

**Expected:**

```json
{"status":"ok","service":"routellm-backend","version":"0.1.0","db":"ok","redis":"ok", ...}
```

`db`/`redis` must both be `ok` — that is the proof Neon and Upstash are wired,
not merely configured.

**d. The full public check (no secrets printed):**

```bash
python scripts/smoke_deployed.py \
  --api https://saboornotgonna-routellm.hf.space \
  --origin https://routellm.saboornasar66.workers.dev
```

**Expected:** `All critical checks passed.` It covers wake-up, `/api/v1/models`,
`/api/v1/catalog`, `/v1/models`, `/` redirect, the CORS preflight for your exact
origin, key generation and a real routed request.

---

## 6) The live dashboard 🔑 — owner only

1. Open `https://routellm.saboornasar66.workers.dev`.
2. The header shows **Connected** (not "backend not running") — that is the
   Worker→Space proxy working.
3. **Keys** page → generate a key → copy it (shown once).
4. **Routing Explorer** → send a prompt with that key → a provider answer comes
   back and the decision appears in the live feed.
5. **Model Health** shows real states. If every model shows `unknown`, `/health`
   will show `db`/`redis` as not-`ok` — go back to §5c.

Browser check for the CORS path:

```text
DevTools → Network → the request to /health has status 200, not (failed) net::ERR
```

---

## 7) Benchmark ⚠️ — the one incomplete deliverable

The committed results are **0/37 successful requests in both arms**, in both
scenarios (`benchmark/results/scenario_{1,2}_results.json`). The runner worked;
the providers did not have usable keys. So the headline "RouteLLM maintains
success during an outage" claim is **not currently supported by the data** — the
README now says so explicitly instead of showing `[X]` placeholders.

To finish this deliverable 🔑:

```bash
# provider keys in backend/.env, backend running with CHAOS_MODE_ENABLED=true
python benchmark/run_benchmark.py --scenario 1
python benchmark/run_benchmark.py --scenario 2
python benchmark/generate_report.py
```

**Expected:** non-zero `success_count` in the RouteLLM arm and, in scenario 2,
a lower success rate for the naive arm than in scenario 1. Then update the table
in `README.md` §"Benchmark Results" with the real numbers.

This is the one place where "done" is genuinely unfinished, and no amount of
code will fix it — it needs live provider keys.

---

## 8) The two red things you'll see, explained

**Cloudflare `Workers Builds: routellm` fails in 0 seconds.** That is a
dashboard-level rejection before any build runs (build/deploy command,
root directory, or compatibility date). It is not a test failure. Local proof
that the config is valid: `npm run deploy:dry-run` (§3). Dashboard settings must
match `docs/CLOUDFLARE.md` §2: root directory `/`, build `npm run build`,
deploy `npm run deploy`, no `--compatibility-date` override.

**Vite chunk-size warning on build.** A performance note about the single JS
bundle. Not a failure; fixing it means code-splitting the dashboard pages, which
is a nice-to-have.

---

## 9) Definition of done

The project is fully done when all of these are true:

- [ ] `pytest` → 332 passed, 2 skipped (§1)
- [ ] `npm test` → 61 passed, `npm run build` → ok, `tsc` clean (§2)
- [ ] `npm run test:cloudflare` → 10 passed, `deploy:dry-run` ok (§3)
- [ ] CI 4/4 green on `main` (§4)
- [ ] `curl …/health` → `{"status":"ok","db":"ok","redis":"ok"}` (§5c)
- [ ] `smoke_deployed.py` → all critical checks passed (§5d)
- [ ] Dashboard connects, generates a key, routes a prompt (§6)
- [ ] Benchmark re-run with real numbers in `README.md` (§7)

Everything except §7 is a configuration/verification step. §7 needs provider
keys; it is the only item that requires new work rather than confirmation.
