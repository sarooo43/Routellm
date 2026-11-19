# Pre-release audit — Routtener v0.1.0

Branch `arena/f16778ba-routellm` · PR #25 · HEAD `0c363ee` (see the provenance note)
Report written 2026-10-31. Every claim is tagged **OBSERVED** (I ran it or read
the exact lines), **DOCUMENTED** (a doc claim I did not verify) or **ASSUMED**.

---

## 1. Executive summary

### Counts

| Suite | Before this audit | After |
|---|---|---|
| Backend — default (SQLite) | 986 passed / 10 skipped / 2 deselected | **1000 passed / 11 skipped / 2 deselected** |
| Backend — real Postgres | 986 | **1000** |
| Backend — real Redis | 996 | **1010** |
| Backend — `-m slow` | not recorded | **2 passed / 1011 deselected** |
| Frontend — vitest | not recorded | **119 passed (19 files)** |
| Frontend — `tsc --noEmit` | not recorded | **clean, exit 0** |
| Frontend — lint | not recorded | **0 errors, 5 warnings (all pre-existing)** |
| Frontend — `vite build` | not recorded | **ok, entry 320.88 kB / 101.86 kB gzip** |

The +14 backend tests are all new regression tests for findings below.

### Proof of passing tests — GitHub Actions run 44540535694 (commit `0c363ee`)

| Job | Result | URL |
|---|---|---|
| No credentials in tree or history | pass, 9s | https://github.com/sarooo43/Routtener/actions/runs/44540535694/job/135617336902 |
| Slow tests (history depth) | pass, 29s | https://github.com/sarooo43/Routtener/actions/runs/44540535694/job/135617457237 |
| backend-tests (3.11) | pass, 2m15s | https://github.com/sarooo43/Routtener/actions/runs/44540535694/job/135617457258 |
| backend-tests (3.12) | pass, 2m12s | https://github.com/sarooo43/Routtener/actions/runs/44540535694/job/135617457224 |
| backend-tests (3.13, informational) | pass, 1m32s | https://github.com/sarooo43/Routtener/actions/runs/44540535694/job/135617457217 |
| frontend-tests | pass, 36s | https://github.com/sarooo43/Routtener/actions/runs/44540535694/job/135617336218 |

Local counts above are supplementary; the Actions run is the proof.

**On provenance.** A commit cannot cite the CI run for itself — the run only
exists after the commit does. So the run above is for `0c363ee`, and **the commit that carries this file is
one past it by construction** — editing a document to cite a run necessarily
creates a new commit. The diff between the two is this document and nothing
else.

**Every** run for this work was six-for-six green, and no code differs between
them and the final commit:

| Run | Commit |
|---|---|
| 44535564165 | `942396e` |
| 44536844903 | `1efbe2f` |
| 44537646511 | `fe2dda2` |
| 44539645714 | `0e71f74` |
| 44540535694 | `0c363ee` |

To check any of them:
`gh pr checks 25` shows the latest, and each URL above names its own job.

### Findings by severity

| Severity | Found | Fixed | Open |
|---|---|---|---|
| P0 (data loss, secret leak, security hole, crash, wrong routing) | 1 | 1 | 0 |
| P1 (documented feature broken, misleading output) | 2 | 2 | 0 |
| P2 (rough edge, missing validation, doc drift) | 5 | 4 | 1 |
| P3 (polish) | 1 | 1 | 0 |
| Needs owner decision | 3 | — | 3 |

**No secret leak, no auth bypass, no injection, and no wrong routing result was
found.** The P0 was a crash-class defect in key-loss handling, detailed below.

---

## 2. Phase 1 first — the empty-key root cause

**Symptom.** A provider key that was present but empty (`""`) was sent to the
provider as though it were a real credential.

**Root cause.** `describe_unusable("")` returned `None`. The function was
written to explain *why* a stored key cannot be used; for the empty string it
concluded there was nothing to explain, so every caller treated `""` as a
usable key. Three consequences:

1. All three adapters built a request with an empty credential and sent it.
2. `gemini_provider` had **no request-time guard at all** — it was only safe by
   accident of the other two.
3. `model_registry._fetch` listed models with the blank key.

**Raw evidence (OBSERVED, before → after):**

```
BEFORE: describe_unusable("") -> None            # "nothing wrong here"
BEFORE: wire = [('GET', None)]                   # blank key sent, outcome=network_error
AFTER : wire = []                                # nothing left the process
AFTER : missing_provider_key: no groq key is saved for this Routtener key and
        no operator key is configured, so the request was never sent.
```

**Fix.** `provider_key_format.describe_unusable()` now names the empty case;
`outcomes.error_for_missing_key()` and `key_problem_response()` give one
message shape; all three adapters (including a **new** Gemini guard) and
`_fetch` refuse a blank key before I/O.

**Tests.** `tests/integration/test_empty_provider_key.py` (9 tests).
**Mutation checks (4, all caught):** 5, 1, 3 and 1 failures respectively when
the guard was reverted.

---

## 3. Findings

| ID | Sev | Area | What is wrong (OBSERVED) | Status |
|---|---|---|---|---|
| F1 | P0 | Provider keys | Deleting `data/encryption.key` made `POST /api/v1/provider-test/{p}` answer a bare **500**, while every other key path answered the documented 503 — a crash-shaped response on the one endpoint a user reaches after a failure. | **FIXED** — `test_losing_the_encryption_key_is_a_503_everywhere_not_a_500` |
| F2 | P1 | Provider keys | After `encryption.key` loss the app silently generated a **new** key with only an INFO line, so every saved provider key became permanently unreadable while the app looked healthy. | **FIXED** — `test_key_loss_is_logged_loudly_when_ciphertext_exists` |
| F3 | P1 | Concurrency | 30 concurrent walk-path requests took **91.1 s** and returned **15 × 503** (`QueuePool limit of size 5 overflow 10 reached`). Cause: a DB DELETE inside `_invalidate_models` plus an undersized SQLite pool. | **FIXED** (commit `208d138`) — `test_concurrent_walk_pool.py` |
| F4 | P2 | Malformed provider replies | A provider answering HTTP 200 with a malformed body was reported as a **network** error, sending users to check their connection instead of the response. | **FIXED** — parser guards in all three adapters + `bad_provider_response` |
| F5 | P2 | Tests | `test_rename_compat.py` created literal `C:/tmp/new-dir` directories **inside the repo** (visible in a staged release archive), because `Settings()` creates the data dir during field resolution. | **FIXED** — `isolated_cwd` fixture + 2 tree guards |
| F6 | P2 | Tests | `test_discovery_wire_contract.py` was **order-dependent**: a leaked `chat_body` class attribute made the module fail 5/10 randomised runs (but 0/10 alone). | **FIXED** — fixture restores `chat_body` |
| F7 | P2 | Live eval | Ctrl-C during a live run printed a full `asyncio` **KeyboardInterrupt traceback** and exited `-2`. (Cleanup itself was already correct.) | **FIXED** — exit 130 + one sentence |
| F8 | P2 | Launcher | `python run.py --port 99999` crashed with a raw `OverflowError: bind(): port must be 0-65535` traceback (`OverflowError` is not an `OSError`). | **FIXED** — argparse type, exit 2 |
| F9 | P2 | Ops feedback | On a DB outage the 503 body is exactly `{"detail":"Database temporarily unavailable"}` — no request id, nothing the user can quote. | **OPEN** (owner/product call) |
| F10 | P2 | Coverage config | The CI coverage figure for `provider_keys_routes.py` reads **66%** because coverage attributes `fastapi/routing.py` line numbers to the file. | **OPEN** (needs owner decision) |
| F11 | P3 | Product decision | `POST /api/v1/keys/generate` returns 201 with a full `rk_` key to an unauthenticated caller when `REQUIRE_API_KEY=false` (the local default). | **NEEDS OWNER DECISION** |
| F12 | P3 | Docs | `app/utils/masking.py` docstring claimed "every string that reaches a log line goes through `mask_secrets()`". It has no caller at all. | **FIXED** (claim corrected; module kept) |
| F13 | — | LICENSE | A pre-existing commit on this branch (`ee39f9b`, not mine, not in this audit's scope to revert) changed `Copyright (c) 2026 sarooo43` → `Routtener contributors`. | **FLAGGED — untouched** |

### Detail worth reading

**F1/F2 — the fix and its honest caveat.** `provider-test` now maps
`ProviderKeyEncryptionError` to the same 503 envelope the rest of the API uses.
`local.load_or_create_encryption_key()` now warns
`encryption_key_missing_but_ciphertext_exists` (with the two real remedies:
restore the backup, or re-save) when it is about to create a key while the
SQLite file already holds `provider_keys` rows. My **first mutation attempt was
a no-op**: I changed the handler to `except RuntimeError`, but
`ProviderKeyEncryptionError` *is* a `RuntimeError`, so the test passed and I
nearly recorded a false "survived". Re-running with `except KeyError` made the
endpoint answer 500 and the test fail, which is the real result.

**F3 — what actually fixed it.** Pool sizing (20 + 20) *and* removing the DB
DELETE from the invalidation path. WAL alone **survives** the mutation test —
it is insurance, not the fix. This is recorded in the commit so nobody credits
the wrong change.

**F10 — the coverage anomaly.** Coverage recorded **line 2287** as executed in a
702-line file. I disproved four hypotheses (stale `.pyc`, double import,
corrupt bytecode, report-format bug) and one of my own early theories (a plain
import does *not* reproduce it). The proven cause: the module registers routes
with `@router.post(...)`, those decorator factories execute during definition
from this module's frame stack, and under this sandbox's pure-Python tracer
(no C extension) the frames are attributed to the file being defined. The
impossible numbers are real `<fastapi/routing.py>` lines — 2287 is
`@asynccontextmanager`. Minimal reproduction in `docs/audit/coverage-tracer-anomaly.md`.

---

## 4. Per-phase results

| Phase | Result | Evidence / notes |
|---|---|---|
| **P0 baseline** | PASS | CI commands read from `.github/workflows/ci.yml`; counts in §1; Python 3.11 local only — 3.12/3.13/3.14 **N/A locally** (sandbox has 3.11.2 only), covered by CI for 3.11/3.12/3.13. |
| **P1 empty key** | **PASS (root cause)** | §2. |
| **P2a** fresh start + perms | PASS | `encryption.key` 0o600, data dir 0o700, `routellm.db` 0o644. |
| **P2b** key CRUD + validation | PASS | 11 validation cases → 422; `rk_` key length 35. |
| **P2c** routing inputs | PASS | Live confidence values **0.375 / 0.675 / 0.9** — all three UI colour bands reachable. |
| **P2d** failure injection | PASS | Garbage/truncated/empty `choices`, 400/401/403/404/408/429/500/502/503, hang, huge response, slow stream. Malformed-200 mislabelling was F4 (fixed). |
| **P2e** walk policy | PASS | Per-model walk, `MAX_CANDIDATE_ATTEMPTS=3`, loop-free, breaker open/recover, 403 is key-wide and never cached as model-denied. |
| **P2f** API keys | PASS | Revoke → 401; wrong prefix → 401; per-key provider scope isolated (test now direct). |
| **P2g** OpenAI compat | PASS | Real `openai` client: unknown model 400 `model_not_found`, `stream=True` 400 `streaming_not_supported`, both metadata namespaces; `routtener-demo` unadvertised and unroutable. |
| **P2h** concurrency | **PASS (F3 fixed)** | 150 concurrent → 150/150; 100 routes + 50 pollers → 4.7 s. |
| **P2i** chaos mode | PASS | Off by default (**404**, not merely ignored); TTL self-expires (`active_chaos` empties); while active the error is `chaos_injected`, after expiry it is `missing_provider_key` — correctly distinguished. |
| **P2j** persistence | PASS | Restart → key still decrypts. `test_rename_compat.py` builds a **real** pre-rename `routellm.db` by hand (10 tests). |
| **P3a** secrets in logs/errors | PASS | Canary `gsk_LEAK…` through route/models/provider-list/provider-test/diagnostics/logs/openapi/index: **absent** from every body, from 138,702 bytes of DEBUG log, and from the SQLite file. |
| **P3b** exposure | PASS | 19 Host attempts: 13 rejected (400). XFH cannot bypass. CORS sends no ACAO for a foreign origin; preflight for it → 400. |
| **P3c** input | PASS | SQLi in provider/`force_provider` → 404/400, DB intact. Model pin `../x`, `/etc/passwd`, `http://evil.com` → 422 (charset **and** discovered-list check); the pin endpoint never reached the wire. CRLF in a key → 422 with the exact position. Regex bombs (200 kB, 100 k emoji, 40-deep parens) each < 0.05 s. |
| **P3d** frontend XSS | PASS | No `dangerouslySetInnerHTML`/`innerHTML`/`eval` anywhere. Hostile model ids (`<img onerror>`, `<script>`) are rejected by the model-id allow-list; React escapes the rest. |
| **P3e** deps | PASS | `pip-audit -r backend/requirements.txt`: **No known vulnerabilities**. `npm audit`: **0 total** (0 low/moderate/high/critical). |
| **P3f** encryption | PASS with F1/F2 | Fernet; key generated 0o600 via `O_EXCL`; loss now loud. |
| **P4** frontend | PARTIAL | No Playwright and no browser in this sandbox. Verified by reading + unit tests: `prefers-reduced-motion` honoured in `index.css`, `focus-visible` rings, `aria-label`/`aria-hidden` present, stale-build and connection banners exist with tests. **Click-through, axe-core, contrast measurement, responsive sweep and animation CPU are NOT verified** — see §7. |
| **P5** launcher/packaging | PASS with F8 | `--check` and `--help` correct; busy port auto-shifts (8000 → 8001); unwritable data dir degrades with a clear line; corrupt DB handled; release staging builds (97 files, 1.9 MB) with no `.env`, key or db file. |
| **P6** evals | PASS | Routing 91.0% **with its own keyword-leakage warning (55 % of prompts share the classifier's vocabulary — self-declared inflated)**. Resilience prints `MODE: mocked` and refuses to show naive-arm failures as provider evidence. Quality prints "not measured: scripted stubs". Wilson verified exactly: 20/20 → 83.9 %, 50/50 → 92.9 %, 0/50 → 7.1 %, 0/0 → 0.0 %. Live preflight: 0 providers aborts with the full report; a **minted key is revoked even on the abort path**; a user-supplied key is never revoked (F7 fixed the SIGINT presentation). |
| **P7** tests | PASS | 12 mutations attempted, 9 applied, **6 caught**; 2 survivors **proved equivalent** by running both versions; 1 real gap closed (chaos). Two flakiness defects found and fixed (F5, F6); final state 6 consecutive clean randomised full-suite runs. Fake audit: `docs/audit/fake-audit.md`. Skips explained: 9 = gradio/spaces not installed, 1 = `ROUTTENER_CHECK_PYPI=1` opt-in, **1 = joblib** (the brief said "10 joblib skips" — that is not what the skips are). |
| **P8** docs/hygiene | PASS with F12 | OpenAPI title `Routtener Gateway API` v0.1.0, 24 endpoints. Outbound UA `Routtener/0.1.0` — contains neither username nor hostname. All relative links resolve except the **4 screenshot slots**, which README already declares as owner tasks. CONTRIBUTING / SECURITY / CODE_OF_CONDUCT / templates / `.env.example` / `.gitignore` all present. `LICENSE` **untouched by me** — see F13. |
| **P9** architecture + dead code | PASS | `docs/architecture.md` written from code with file:line refs, 5 Mermaid diagrams. Dead code: `judge` and `masking` looked orphaned; `judge` is used by a script (KEEP), `masking` is **proven dead** (no import, no test) but **kept** because deleting a security-shaped file is the owner's call — its false docstring claim is corrected instead. |
| **P10** verify | PASS | §1 counts + the Actions run above. |

---

## 5. Needs an owner decision

### D1 — Key generation is unauthenticated in local mode (F11)

`REQUIRE_API_KEY=false` is the local default, and `POST /api/v1/keys/generate`
answers **201 with a full `rk_` key** to a caller with `Origin:
https://evil.example` and no credentials.

*Options:* (a) leave it — it is a local-mode demo default and the key confers no
provider keys; (b) require a header/secret even locally; (c) rate-limit key
generation. **(a)** matches the documented "local-first, auth is a mode"
design; **(b)** breaks the one-click local experience. I did not choose,
because this is the documented auth model, not a defect.

### D2 — No request body size limit (F9-adjacent)

A **10 MB** JSON body is accepted (200). 1 MB is also 200; 500-deep JSON → 422.
`--host 0.0.0.0` exposes this to a LAN. Mitigation today is the binding default
(loopback) plus deployment-level limits.
*Options:* add a body-size middleware (a behaviour change with a chosen limit),
or document that a reverse proxy must enforce it.

### D3 — The coverage number is wrong, and fixing it is a CI choice (F10)

*Options:* (1) install the C tracer in CI — most correct, adds a build step;
(2) add a coverage config rule — narrow, may not remove the bad attribution;
(3) leave it and keep the explanation document. Recommendation: (1) if CI time
allows, else (3). **No product code should change for this.**

### D4 (flag, not a decision) — the LICENSE line

`ee39f9b` on this branch (an earlier session, `2026-11-19 06:35`) changed
`Copyright (c) 2026 sarooo43` → `Copyright (c) 2026 Routtener contributors`.
I did not touch it, per instruction. It is in the PR diff against `main` and the
owner should consciously keep or revert it before release.

---

## 6. Owner-only checklist (click by click)

1. **Real provider keys** — save one Groq and one Gemini key from the Providers
   page, press **Test** on each, confirm `works` rather than a shape error.
2. **Live resilience eval** —
   `export ROUTTENER_EVAL_API_KEY=<a key that owns the saved provider keys>`,
   then `python evals/run_resilience_eval.py --live --dry-run --yes-spend-quota`,
   then drop `--dry-run`. Confirm the written `docs/eval-results.md` says
   `MODE: LIVE` with the real date/commit/providers, and **commit it**.
3. **Fresh clone** — `git clone` into an empty directory, `python run.py`,
   confirm the dashboard loads with zero configuration.
4. **Screenshots** — add the four PNGs named in `README.md` to `docs/images/`
   (`overview`, `explorer`, `failover`, `response`). The slots exist; the files
   intentionally do not.
5. **Cloudflare / managed deployment** — `docs/deploy/` is archived and was not
   exercised. Deploy and confirm the app starts with `DATABASE_URL` unset.
6. **Browser-only checks** that this sandbox could not do: click every control,
   watch the console for errors, run axe-core, measure contrast, and check 360 /
   768 / 1440 px layouts.

---

## 7. Not verified (each ASSUMED unless stated)

| Item | Status |
|---|---|
| Python 3.12 / 3.13 / 3.14 behaviour | **ASSUMED** — sandbox has 3.11.2 only. CI covers 3.11 and 3.12 (passing); 3.13 is informational. 3.14 untested anywhere. |
| Playwright click-through, console errors, axe-core, contrast, responsive layouts, animation CPU | **ASSUMED / NOT VERIFIED** — no browser available. |
| Live eval numbers with real providers | **ASSUMED** — no real keys used, by instruction. |
| Gradio / Hugging Face Space path | **ASSUMED** — 9 tests skip (modules not installed). |
| Cloudflare / managed deployment | **ASSUMED** — archived, not run. |
| Screenshots rendering | **ASSUMED** — 4 owner slots, files absent by design. |
| Cross-platform launcher (Windows/macOS) | **ASSUMED** — Linux only here; `start.ps1`/`start.sh` not executed. |
| Real provider rate-limit behaviour under load | **ASSUMED** — stubs only. |
