# Verification audit — `audit/verification` (branch from origin/main @ `35898f9`)

Audit date: 2026-11-16. Every claim below is labelled **OBSERVED** (this audit ran the
command and shows raw output), **DOCUMENTED** (a repo file/CI page says so), or
**ASSUMED**. Sandbox limits: the machine running the audit HAS network for PyPI and
git (proven by fresh venv installs and a fresh clone), but NO provider credentials —
every live-provider claim is **NOT VERIFIABLE BY YOU (the auditor)**.

Legend for the tables: `P` = passed, `S` = skipped, `D` = deselected.

---

## A1. Inventory

### Backend — 815 tests / 55 files (**OBSERVED**, `pytest --collect-only -q`:
`815/817 tests collected (2 deselected) in 15.19s`)

| Test file | # | Area |
|---|---|---|
| tests/integration/test_routes.py | 10 | routing + logs/analytics endpoints |
| tests/integration/test_walk_policy.py | 10 | walk policy, fallback, 404/403/429 memory |
| tests/integration/test_openai_compat.py | 30 | OpenAI-compat endpoints |
| tests/integration/test_repository_hygiene.py | 27 | repo hygiene (secrets/sizes) |
| tests/unit/test_tolerant_parsers.py | 23 | provider error classification |
| tests/integration/test_packaging.py | 19 | packaging/startup |
| tests/integration/test_deployment_resilience.py | 19 | deployment/health probes |
| tests/integration/test_auth_matrix.py | 18 | API-key auth matrix |
| tests/integration/test_frontend_serving.py | 16 | static frontend serving |
| tests/integration/test_demo_mode.py | 15 | demo/mock mode |
| tests/integration/test_model_explorer.py | 13 | explorer + pin validation |
| tests/integration/test_redis_contract.py | 13 | quota/redis contract |
| tests/integration/test_provider_test_endpoint.py | 19 | provider test buttons |
| tests/unit/test_model_discovery.py | 24 (25 incl. new) | discovery + model-id validation |
| tests/integration/test_provider_keys.py | 11 | BYOK key save/use/isolation |
| tests/integration/test_fallback_flow.py | 11 | fallback chain |
| tests/integration/test_feedback.py | 10 | feedback endpoint |
| tests/integration/test_chaos_executor.py | 10 | chaos gate |
| tests/integration/test_logging.py | 8 | log hygiene |
| tests/integration/test_api_keys.py | 8 | API-key CRUD |
| tests/integration/test_breaker_policy.py | 8 | circuit breaker |
| tests/integration/test_cors_and_failsoft.py | 4 | CORS + fail-soft |
| tests/integration/test_failure_messaging.py | 6 | user-facing failure text |
| tests/integration/test_provider_diagnostics.py | 6 | in-app diagnostics + redaction |
| tests/integration/test_forced_mock_demo.py | 4 | forced mock + BYOK |
| tests/integration/test_model_cache_db.py | 5 | discovery cache persistence |
| tests/integration/test_no_auto_route_after_save.py | 2 | no silent auto-route |
| tests/integration/test_no_secret_leaks_route.py | 1 | route logs never leak keys |
| tests/integration/test_key_managers.py | 14 (**NEW this audit**) | key managers (A5) |
| tests/unit/test_no_secret_leaks.py | ~7 | redaction unit tests |
| tests/unit/test_allowed_hosts_middleware.py | 15 | host guard |
| tests/unit/test_gemini_auto_allowlist.py | n | allow-list (Auto) |
| tests/unit/test_check_secrets.py | 4 (2 slow) | secret scanner |
| tests/unit/test_error_messages.py | n | error message shaping |
| tests/unit/test_ml_classifier.py | n | ML classifier |
| tests/unit/test_local.py | n | local-mode bits |
| tests/test_deploy_scripts.py, test_run_launcher.py, test_hf_app.py, test_hf_requirements.py | n | deploy scripts / HF Space |
| (remaining files: misc unit/integration) | n | — |

Exact per-file counts for every one of the 55 files were captured in the audit
session (`--collect-only`); the table above lists the ones mapped to named areas.

### Frontend — 18 test files / 112 tests (**OBSERVED**, `npx vitest run`:
`Test Files 18 passed (18)`, `Tests 112 passed (112)`)

(`vitest list` prints 113 entries; one is a spurious parse of an `expect` line
containing `freeIds.includes(id)` — static grep counts 112 `it(`/`test(` blocks
and the runner agrees.)

### Areas with ZERO tests — listed plainly (**OBSERVED**)

Frontend components/pages with no dedicated test file:
`pages/Analytics.tsx`, `pages/ModelHealth.tsx`, `pages/Overview.tsx`,
`components/ChaosBanner.tsx`, `components/FallbackChainGraph.tsx`,
`components/GlassCard.tsx`, `components/Layout.tsx`,
`components/PageHeader.tsx`, `components/Skeleton.tsx`,
`components/Topbar.tsx`, `components/TryItLive.tsx`.

Backend: no requirement area was found with zero tests; the thinnest areas
are UI-facing static pages above and the deploy scripts (platform-skipped, see A3).

---

## A2. Everything was run — raw results

| Run | Result (**OBSERVED**) |
|---|---|
| Backend default suite, Python 3.12, RUN 1 | `800 passed, 15 skipped, 2 deselected in 417.11s` |
| … RUN 2 | `800 passed, 15 skipped, 2 deselected, 1 warning in 274.24s` (warning: `PytestUnhandledThreadExceptionWarning`, see B) |
| … RUN 3 | `800 passed, 15 skipped, 2 deselected in 238.22s` |
| … RUN 4 (warning hunt) | `800 passed, 15 skipped, 2 deselected in 206.88s` — warning did NOT reproduce |
| Backend suite, Python 3.11 | `799 passed, 16 skipped, 2 deselected in 227.36s` |
| Backend suite, Python 3.13 | `799 passed, 16 skipped, 2 deselected in 317.56s` |
| Python 3.14 | **NOT RUN — not installed on the audit machine** |
| `pytest -m slow` | `2 passed, 815 deselected in 706.70s` (`test_repository_scan_never_raises_on_binary_history`, `test_history_scan_is_clean_or_reports_nothing_actionable`) |
| Frontend `vitest run` | `Test Files 18 passed (18)`, `Tests 112 passed (112)` in 146.93s |
| Frontend `tsc -b` | exit 0 (**OBSERVED**: `TSC EXIT: 0`) |
| Frontend `oxlint` | exit 0 — 4 warnings in `ModelExplorerPanel.tsx` only (set-state-in-effect ×2, exhaustive-deps ×2) |
| Frontend `vite build` | exit 0 — `✓ built in 37.55s` |
| `ROUTELLM_CHECK_PYPI=1 pytest tests/test_hf_requirements.py` | `8 passed in 2.65s` (PyPI reachable from the audit sandbox) |

No test changed pass/fail across the three default-suite runs → **no flaky
failures** (**OBSERVED**). The RUN 2 warning is analysed in section B.

---

## A3. Skip / xfail audit

All skip reasons printed with `-rs` under Python 3.13 (**OBSERVED**; 3.12 has the
same list minus one, see below):

| Skip site | Reason (verbatim from `-rs`) | Still true? |
|---|---|---|
| tests/test_deploy_scripts.py:213 | bash here is the WSL stub with no distro installed… runs on Linux CI | yes (win32 sandbox) |
| tests/test_hf_app.py:128,137,162,184,646 | could not import 'gradio' | yes (optional dep not installed) |
| tests/test_hf_app.py:229 | Windows SO_REUSEADDR permits rebinding… Linux deploy host | yes |
| tests/test_hf_app.py:298 | Windows SO_REUSEADDR permits a second bind… Linux deploy host | yes |
| tests/test_hf_app.py:532,561,577,595 | could not import 'spaces' | yes (optional dep) |
| tests/test_hf_requirements.py:121 | set ROUTELLM_CHECK_PYPI=1 to query PyPI | opt-in; **verified passing when enabled** (A2) |
| tests/test_run_launcher.py:141 | Windows SO_REUSEADDR lets port_is_free's probe bind… Linux in production | yes |
| tests/unit/test_local.py:140 | POSIX permission bits only | yes (win32) |
| tests/unit/test_ml_classifier.py:87 | could not import 'joblib' | environmental: the fresh 3.11/3.13 audit venvs lack `joblib`; the 3.12 venv has it → the 3.12 run has 15 skips, 3.11/3.13 have 16 (**OBSERVED**) |

No xfail markers exist in the backend tests, and no `.skip`/`.todo`/`.fixme`
exist in the frontend sources (**OBSERVED**, grep). **No skips were removed** —
every reason is still true in this environment; none is stale.

---

## A4. Do the tests actually test? Mutation sanity check

Method: apply the change, run the targeted test set, record failing test names,
restore, `git diff` clean (**OBSERVED** for each row: `DIFF LINES: 0` after every
restore except the new test files this audit adds on purpose).

| # | Change applied | Caught by (named tests that FAILED) | GAP? |
|---|---|---|---|
| 1 | 404 classified as auth error (`outcomes.py` `not_found → auth_failed`) | `test_tolerant_parsers.py::test_gemini_cohort_404_is_model_not_found_with_replacement_hint`, `::test_groq_decommissioned_404_is_model_not_found_not_auth` (2 failed, 48 passed) | no |
| 2 | walk stops after first 404 (`while False and …`) | `test_walk_policy.py`: retired_404_walks_to_replacement_named_in_full_body, replacement_hint_to_an_excluded_id_is_ignored, walk_is_capped_at_three_attempts_per_request, 403_probes_one_other_family_then_stops, last_known_good_model_is_tried_first, statuses_persist_to_the_db_for_restart (6 failed, 4 passed) | no |
| 3 | redaction disabled (`redact()` early return) | `test_no_secret_leaks.py` ×6 (provider_echo…[groq|gemini|deepseek], query_string_echo, bearer_and_basic_headers, connection_test_response), `test_no_secret_leaks_route.py::test_logs_written_during_a_failed_route_are_clean`, `test_tolerant_parsers.py::test_capture_writes_field_names_and_redacted_sample`, `::test_capture_of_an_error_response_is_also_redacted` (9 failed) | **finding**: `outcomes.scrub()` is dead code (defined, never called — the live path is `redact()` + `provider_keys_routes._scrub`). Mutating the dead function caught nothing; mutating the live function caught 9 tests. Dead code noted in Known gaps. |
| 4 | Host guard allow-all (`_host_allowed` → `return True`) | `test_allowed_hosts_middleware.py`: unknown_host_is_rejected, missing_host_header_is_rejected, env_override_adds_deployed_hostnames, codespaces_foreign_space_is_rejected, non_codespaces_env_unchanged, codespaces_true_without_a_name_fails_closed, codespace_rejection_message_names_the_host_and_the_fix, ipv6_without_brackets_is_not_corrupted_by_port_splitting, unknown_host_gets_400_from_the_app, loopback_host_gets_through_to_the_app (10 failed) | no |
| 5 | mock-provider BYOK gate reintroduced (`executor.py` drop `and provider_name != "mock"`) | `test_forced_mock_demo.py::test_forced_mock_succeeds_even_with_a_stored_byok_key` (1 failed) | no |
| 6 | `-tts` model allowed into Auto (`is_chat_capable` → `return True`) | `test_model_discovery.py::test_chat_capability_filter[whisper-large-v3|nomic-embed-text|text-embedding-004]`, `::test_openai_shaped_list_is_filtered_and_ordered` (4 failed) | no |
| 7 | pin validation accepts `../x` (drop `".." in model_id` check) | **NOTHING failed** — `test_pin_rejects_injection_attempts` still passed because the pin endpoint's SECOND validation layer (discovered-list membership) independently rejects `../x`. **GAP → test added:** `test_model_discovery.py::test_is_valid_model_id_rejects_path_traversal_and_urls` pins the charset layer directly. Under the mutation it FAILS (`assert not is_valid_model_id("../x")` → `assert not True`); after restore it PASSES (25 passed). | **yes → fixed** |

---

## A5. Coverage

Backend, full suite including this audit's new tests (**OBSERVED**,
`pytest --cov=app --cov-report=term`, 3.12):
`TOTAL 4128 672 84%` — `815 passed, 15 skipped, 2 deselected`.

Critical files, least-covered first (**OBSERVED** from the same report):

| File | Stmts | Miss | Cov |
|---|---|---|---|
| app/api/provider_keys_routes.py | 254 | 87 | 66% * |
| app/api/setup_routes.py | 46 | 15 | 67% |
| app/api/routes.py | 305 | 92 | 70% |
| app/core/model_discovery.py | 503 | 146 | 71% |
| app/api/catalog_routes.py | 77 | 16 | 79% |
| app/main.py | 142 | 25 | 82% |
| app/providers/outcomes.py | 121 | 18 | 85% |
| app/core/executor.py | 284 | 39 | 86% |
| app/providers/model_registry.py | 456 | 51 | 89% |
| app/api/keys_routes.py | 47 | 4 | 91% |

\* `provider_keys_routes.py` carries a **coverage-tool artifact**, documented in
Known gaps: the handlers execute (proved by a temporary in-handler print that
appeared during a test run) while the tracer does not record many of their lines,
and records phantom line numbers (e.g. 1981, 2287) under this filename for a file
that is 656 lines long. Direct in-process handler calls DO record correctly.
Every reported-missed region nonetheless has passing named tests: list/status
(`test_provider_keys.py`), delete cascade
(`test_upsert_updates_existing_row_and_delete_removes_it`), pin set/unset
(`test_model_explorer.py::test_pin_valid_model_…`, `test_pin_null_returns_to_auto`),
classify (`test_provider_test_endpoint.py`), diagnostics
(`test_provider_diagnostics.py`, 6 tests).

**Tests added this audit for critical files under 70%:**

- `tests/integration/test_key_managers.py` — 14 tests (**OBSERVED**:
  `14 passed in 2.04s`). `api_key_manager` 56% → **100%**, `provider_key_manager`
  65% → **100%** (focused `--cov` run: both `50 0 100%` / `52 0 100%`).
- `tests/unit/test_model_discovery.py::test_is_valid_model_id_rejects_path_traversal_and_urls`
  — the mutation-7 gap test.

Frontend coverage (**OBSERVED**, `npx vitest run --coverage` with
`@vitest/coverage-v8` installed `--no-save`): `Test Files 18 passed (18)`,
`Tests 112 passed (112)`, `All files 81.99% stmts / 74.92% branch / 80.99%
funcs / 84.48% lines`. Least-covered: `src/api/client.ts` 27.4% stmts,
`src/api/workerDetail.ts` 37.5%, `src/api/navigation.ts` 42.9%,
`src/api/errors.ts` 58.2%, `src/components/KpiCard.tsx` 66.7% (its test file
exercises render, not the branch), `src/pages/RoutingExplorer.tsx` 76.4%.

---

## A6. Contract / integration tests through real HTTP

Every required flow exists through the real endpoint (httpx.AsyncClient +
ASGITransport, upstreams stubbed) — **OBSERVED** in test sources:

| Flow | Test |
|---|---|
| `/api/v1/route` success | `test_routes.py::test_post_route_success_exact_shape` |
| `/api/v1/route` 404 walk | `test_walk_policy.py::test_retired_404_walks_to_replacement_named_in_full_body` |
| `/api/v1/route` 429 stop | `test_walk_policy.py::test_429_stops_the_walk_and_parks_the_model_until_retry_after` |
| key-wide 403 | `test_walk_policy.py::test_403_probes_one_other_family_then_stops`, `::test_403_with_no_other_family_stops_without_a_probe` |
| `/v1/chat/completions` | `test_openai_compat.py` (30 tests, e.g. `test_auto_completion_runs_pipeline_and_matches_openai_schema`) |
| `/v1/models` | `test_openai_compat.py` |
| provider-key save / test / pin | `test_provider_keys.py`, `test_provider_test_endpoint.py`, `test_model_explorer.py` (pin flow) |
| BYOK + forced mock | `test_forced_mock_demo.py::test_forced_mock_succeeds_even_with_a_stored_byok_key` |
| bad Host header | `test_allowed_hosts_middleware.py::test_unknown_host_gets_400_from_the_app` |
| bad pin input | `test_model_explorer.py::test_pin_rejects_injection_attempts` |

Nothing was missing → no new A6 tests needed.

---

## A7. Secrets

- Tree + history scan (**OBSERVED**): `python backend/scripts/check_secrets.py` →
  `No credentials found in the tree + history. Findings are masked; rotate
  anything that ever appears here.` — exit 0, across all 105 commits.
- Echo-key protection already on main, and proven live by mutation 3 (A4):
  `test_no_secret_leaks.py::test_provider_echo_in_an_error_body_is_redacted[...]`,
  `test_no_secret_leaks_route.py::test_logs_written_during_a_failed_route_are_clean`,
  `test_provider_diagnostics.py::test_diagnostics_never_leaks_a_key_in_response_logs_or_report`
  — all FAILED when redaction was disabled, all pass on the restored code.

---

## A8. Fresh clone, no env vars

**OBSERVED**: `git clone --depth 1 https://github.com/sarooo43/Routellm.git`
→ `CLONE EXIT: 0`, `35898f9 Model Explorer: retire-by-default Gemini ranking…`.
No `ROUTELLM*`/`DATABASE_URL`/`REDIS_URL` env vars present (script printed none).
`python run.py` (Python 3.13): installed 257 packages, built the frontend
(`✓ 3454 modules transformed`, dist emitted), then:

```
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     127.0.0.1:56710 - "GET /health HTTP/1.1" 200 OK
INFO:     127.0.0.1:56710 - "GET /v1/models HTTP/1.1" 200 OK
```

- `GET /health` → **200**, body: `{"status":"ok","service":"routellm-backend","version":"0.1.0","uptime_s":357.576,"db":"ok","redis":"ok","components":{"redis":"ok","database":"ok"}}`
- `GET /docs` → **200**
- Server process stopped afterwards.

---

## A9. Per-area summary

| Area | Tests | Pass/Fail/Skip | Run URL | NOT covered |
|---|---|---|---|---|
| Routing core (/api/v1/route) | 10 | 10 P | local (this audit) | live-provider routing |
| Walk policy / fallback / breaker | 10+11+8 | 29 P | local | live-provider walk |
| Quota / Redis contract | 13 | 12 P, 1 S (no real Redis) | local | real Redis |
| Provider errors / classification | 23+6 | 29 P | local | live provider error bodies |
| Explorer / pin | 13+6 | 19 P | local | live discovery lists |
| Key handling + redaction | ~22+14 new | 36 P | local | real provider keys |
| Host guard | 15 | 15 P | local | reverse-proxy headers |
| Mock / demo | 15+4 | 19 P | local | — |
| OpenAI-compat | 30 | 30 P | local | — |
| API keys / auth | 8+18 | 26 P | local | — |
| UI (frontend) | 112 | 112 P | local | Analytics/ModelHealth/Overview pages + 7 components (no test files) |
| Deploy scripts / HF | — | 10 S (platform) | local | Linux-side behaviour |
| CI (GitHub Actions) | — | — | https://github.com/sarooo43/Routellm/actions | see "CI" below |

**CI**: no Actions run exists for branch `audit/verification` — CI triggers on
push/PR targeting `main`, and this audit pushed a branch only. Baseline evidence
(**DOCUMENTED**): main's CI run #47 was green at `35898f9`. The auditor had no
`gh` CLI; the Actions page is the owner's check (section E).

## Needs a live run by the owner

Only the owner's real keys can verify these (**NOT VERIFIABLE BY YOU — the audit
sandbox holds no provider credentials**):

1. **Gemini**: a real `AIza…` key lists models (`fetch_counts` counts), a retired
   model 404 walks to the replacement Gemini suggests, and 403
   PERMISSION_DENIED is shown as a restriction, not "bad key".
2. **Groq**: a real `gsk_…` key survives the `/v1/models` + chat round-trip; a
   decommissioned-model 404 classifies as `model_not_found` (unit tests pin the
   *parsing*; the live response shape is owner-verified).
3. **DeepSeek**: a real key confirms the 400 "Model Not Exist" body still maps to
   `model_not_found`, and the OpenAI-shaped list parses with the raw count.
4. In-app diagnostics against the three live providers: table, clipboard report
   and redaction on REAL provider bodies.
5. The Hugging Face Space deploy path (Linux-only behaviours skipped on Windows).

## Known gaps

1. **`outcomes.scrub()` is dead code** — defined, never called. Harmless but
   misleading; the live redaction path is `redact()` + `_scrub()` in
   `provider_keys_routes.py`. (Found by mutation 3; left in place — audit adds no
   features/removals.)
2. **Pin-validation charset layer had no direct test** — masked by the
   discovered-list layer. Fixed this audit (mutation-7 test).
3. **Coverage under-reporting for `provider_keys_routes.py`** — follow-up T5
   tested the two hypotheses the owner raised: there was NO coverage config
   (`.coveragerc` now added with `concurrency = thread`; `parallel` stays off so
   `.coverage` is always overwritten, never merged with stale data — confirmed
   via `coverage debug config`: `concurrency: thread`, `parallel: False`, and the
   stale `.coverage` was deleted before rerunning). The report is UNCHANGED
   (66%). Four facts pin the artifact on the tracer, not the tests: (a) no
   version of the file in any ref ever exceeded 569 lines (`git log --all`
   line-count dump), yet the data file records lines up to 2287 under this
   filename; (b) `gc` scan finds no live phantom code objects after a run;
   (c) direct in-process handler calls DO record (lines 108–110 and 576–578/580
   recorded in probe 3); (d) three NEW endpoint tests through
   `httpx.ASGITransport` (`tests/integration/test_provider_keys_endpoints.py`)
   execute exactly the reported-missed lines — list happy path (109–110), DELETE
   cascade with seeded pin+status rows (159–178), diagnostics unknown-provider
   404 (579) — and the report STILL marks them missed in the same data file.
   The reported number is therefore understood as a floor; every region has
   named passing tests.
4. **Frontend components/pages without tests** — listed in A1.
5. **`joblib` missing in fresh dev venvs** — one extra skip on 3.11/3.13
   (`test_ml_classifier.py:87`); installing `joblib` (in requirements or docs)
   would remove the 3.11/3.13-vs-3.12 count difference.
6. **Python 3.14 not tested** — not installed in the audit sandbox.

---

## Follow-up (2026-11-18): timing contradiction, skip accounting, cloud refs

### T2 — why the old numbers (2.8 s / 23 s) and the audit numbers (706 s / 227–318 s) differ

`pytest.ini` itself documents the origin of the small numbers: the slow
marker comment says "it was **2.8 s of a 28 s suite on the machine this was
measured on**". Those numbers come from the author's environment — a
different machine and, crucially, a SHALLOW git history (the history secret
scan is O(history depth); on a fresh 100+-commit clone with full depth it
walks hundreds of MB of blobs). The audit machine measured, same code:

- `pytest -q --durations=25` (default suite, full-depth clone, 3.12):
  `815 passed, 15 skipped, 2 deselected, 1 warning in 358.34s`, slowest:
  `51.89s test_check_secrets.py::test_the_repository_has_no_credentials_in_the_tree`,
  `44.91s test_repository_hygiene.py::test_no_dangling_references_to_the_old_docs_paths`,
  `35.82s test_deploy_scripts.py::test_qualified_refspec_push_succeeds`,
  `9.03s test_routes.py::test_rate_limit_kicks_in_after_31_requests`.
- `pytest -m slow --durations=10`:
  `224.92s test_history_scan_is_clean_or_reports_nothing_actionable`,
  `218.39s test_repository_scan_never_raises_on_binary_history`,
  `2 passed, 830 deselected in 485.66s`.

What differs between the environments (**ASSUMED from evidence in repo**):
machine speed ( Codespace vs this Windows box), git history depth (shallow
checkout vs `fetch-depth: 0` full object database), cold pip/npm caches,
and whether `.venv` was already built. Which number applies where: the
**706 s slow + ~4–6 min default** numbers apply to a normal contributor
laptop cloning the repo at full depth; CI runs the history scan with
`fetch-depth: 0` too, so CI behaves like the audit numbers, not like the
2.8 s/28 s numbers. CI job durations: recorded from the PR run URL in the
final report of this follow-up.

### T3 — skip accounting (11 → 15/16)

The default suite now reports **15 skipped + 2 deselected** (3.12) and
**16 skipped + 2 deselected** (3.11/3.13 fresh venvs). All mechanisms,
with what changed in PR #16 (`git grep` against `136fae0` and `35898f9`):

| Skip site | Reason | Since #16? |
|---|---|---|
| test_deploy_scripts.py:213 | bash here is the WSL stub… (win32) | existed before |
| test_hf_requirements.py:121 | set ROUTELLM_CHECK_PYPI=1 to query PyPI | existed before (opt-in; verified passing) |
| unit/test_local.py:140 | POSIX permission bits only | existed before |
| test_hf_app.py ×9 | could not import 'gradio'/'spaces' | existed before (importorskip sites unchanged) |
| unit/test_ml_classifier.py:87 | could not import 'joblib' | existed before |
| test_run_launcher.py:141 | Windows SO_REUSEADDR… Linux in production | **NEW in #16** |
| test_hf_app.py:229, :298 | Windows SO_REUSEADDR… Linux deploy host | **NEW in #16** |
| integration/test_deployment_resilience.py:378 | WinError 10106 (win32 socket) | **NEW in #16** |
| (2 history-scan tests) | now DESELECTED by `addopts = -m "not slow"`, run as the dedicated CI `-m slow` job | **NEW in #16** (pre-#16 pytest.ini had no addopts) |

The 16th skip (3.11/3.13 only) is the same `joblib` importorskip — the
fresh audit venvs had not installed it. **joblib is intentionally
optional**: `backend/requirements-ml.txt` says "OPTIONAL — the app runs
fine without these"; `ml_classifier.py` imports it lazily so the app must
start cleanly without it; and the no-joblib failure path ALREADY has a
test that runs without joblib
(`test_load_missing_ml_deps_is_graceful` — simulates `ImportError`, runs
in CI). The corrupt-pickle test (`:87`) is the only one that needs the
real joblib and importorskips in CI — documented as intentionally optional,
no install change made.

CI runs for this PR (**OBSERVED** via the GitHub API):

- Run #48 @ `ba7bb07` — https://github.com/sarooo43/Routellm/actions/runs/44176824524
  — `completed / success`, 8/8 jobs green: No credentials 11s, frontend 42s,
  hf-space-boot 50s, cloudflare-worker 28s, Slow tests (history depth) 35s,
  backend-tests 3.13 44s, 3.11 79s, 3.12 89s.
- Run #50 @ `2295d01` — https://github.com/sarooo43/Routellm/actions/runs/44177691849
  — `completed / success`, 8/8 jobs green (durations above unchanged in kind:
  backend 3.12 job 79s incl. install, suite `832 passed, 11 skipped, 2
  deselected, 227 warnings in 42.29s`).

CI backend suite is ~4–7× faster than the audit Windows box (42–50 s vs
227–358 s; the full-depth history scan `2 passed, 833 deselected in 10.52s`
vs 218–225 s per test locally) — CI, not the audit laptop, is the reference
for contributor timing; the 2.8 s/28 s figures in pytest.ini's comment were
measured on the author's machine with a shallower history.

**Skip count and reasons printed BY CI** (run #50, `pytest -rs` added to the
coverage job in `2295d01`, 3.12 job log, verbatim):

```
SKIPPED [1] tests/test_hf_app.py:128: could not import 'gradio': No module named 'gradio'
SKIPPED [1] tests/test_hf_app.py:137: could not import 'gradio': No module named 'gradio'
SKIPPED [1] tests/test_hf_app.py:162: could not import 'gradio': No module named 'gradio'
SKIPPED [1] tests/test_hf_app.py:184: could not import 'gradio': No module named 'gradio'
SKIPPED [1] tests/test_hf_app.py:532: could not import 'spaces': No module named 'spaces'
SKIPPED [1] tests/test_hf_app.py:561: could not import 'spaces': No module named 'spaces'
SKIPPED [1] tests/test_hf_app.py:577: could not import 'spaces': No module named 'spaces'
SKIPPED [1] tests/test_hf_app.py:595: could not import 'spaces': No module named 'spaces'
SKIPPED [1] tests/test_hf_app.py:646: could not import 'gradio': No module named 'gradio'
SKIPPED [1] tests/test_hf_requirements.py:121: set ROUTELLM_CHECK_PYPI=1 to query PyPI
SKIPPED [1] tests/unit/test_ml_classifier.py:87: could not import 'joblib': No module named 'joblib'
========= 832 passed, 11 skipped, 2 deselected, 227 warnings in 42.29s =========
```

On Linux CI the four win32 SO_REUSEADDR skip sites and the POSIX/deploy-script
tests RUN instead of skipping (843 collected vs 833 selected on Windows), which
is why CI counts 11 while Windows counts 15/16.

### T6 — origin/cleanup/v0.1.0 and cloud leftovers

`git log origin/main..origin/cleanup/v0.1.0 --oneline` (**OBSERVED**):

```
a15fe15 T3 follow-up: hygiene tests assert the new no-shipped-numbers contract
7918fee T4: repo hygiene - changelog, env examples, no release tag
842e1eb T3: one-command benchmark, report with date and models used
ac48020 T2: remove cloud leftovers from CI, archive the worker code
2489f2c T1: stale build detection - banner when bundle and backend commits differ
```

`git merge-base --is-ancestor origin/cleanup/v0.1.0 origin/main` → exit 1:
**NOT merged into main.** Its commit `ac48020` is exactly the "remove cloud
leftovers from CI" change, which is why main still carries them.

Still ACTIVE on main (reported, nothing removed):

- `.github/workflows/ci.yml`: job `hf-space-boot` (lines 165–280; boots
  `hf_app.py`, probes public paths), job `cloudflare-worker` (289–304;
  `npm run test:cloudflare`), step "Verify the Hugging Face entrypoint
  imports" (91–92), step "Validate Wrangler deploy config" (309).
- `.github/workflows/deploy-hf.yml`: full HF Space deploy pipeline
  (inert without the `HF_TOKEN` secret; pushes to
  `huggingface.co/spaces/saboornotgonna/routellm`).
- `.github/workflows/keepalive.yml`: "Keep the Space awake"
  (targets `https://saboornotgonna-routellm.hf.space`).
- `docs/testing.md`: documents the gradio/spaces skips as Space-runtime
  only; `docs/deploy/HUGGINGFACE_AND_MANAGED.md` referenced by the deploy
  workflow. A `cloudflare/` directory exists at the repo root (archived on
  the cleanup branch).
