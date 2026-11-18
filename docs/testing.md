# Testing

Everything here is measured, not asserted. Numbers below were produced on the
machine that last changed this file; re-run the commands to update them.

## The two suites

```bash
# Backend — from backend/
cd backend
python -m pytest                 # default: ~24 s, 804 passed, 11 skipped, 2 deselected
python -m pytest -m slow         # the history scan only: ~7 s, 2 passed

# Frontend — from frontend/
cd frontend
npm ci
npx vitest run                   # 112 passed in 18 files
npm run build                    # tsc -b && vite build; initial chunk 319 kB
```

`pytest.ini` sets `addopts = -m "not slow"`, so the expensive history scan is
excluded from the default run. CI has a dedicated `slow-tests` job that runs
`pytest -m slow` with `fetch-depth: 0` (the scan needs real history, not a
shallow clone). Nothing is skipped silently: the `slow-tests` job is required,
so a failing history scan still fails the build.

## Slow tests

| Marker | Where | Cost | Why it is slow |
|---|---|---|---|
| `@pytest.mark.slow` | `tests/unit/test_check_secrets.py::test_repository_scan_never_raises_on_binary_history` | ~2.8 s | walks every blob in the git object database |
| `@pytest.mark.slow` | `tests/unit/test_check_secrets.py::test_history_scan_is_clean_or_reports_nothing_actionable` | ~2.8 s | same full-graph scan |

Run them explicitly:

```bash
cd backend && python -m pytest -m slow -q
```

Measured default-suite wall time before the marker existed: **28.32 s**
(806 passed, 11 skipped). After: **23.56 s** (804 passed, 11 skipped,
2 deselected). The 2.8 s difference is those two scans; the total is not
"19 minutes" as one earlier report claimed — that figure did not reproduce
here, and the marker is justified by the fact that this cost grows with
history, not by the absolute number.

## Skips

Skip counts differ by platform. OBSERVED in the CI `backend-tests` job log
(run `44221303211`, Python 3.11 and 3.12): **11 skipped** on the Linux host
before this branch. The Windows-local run reports **15**, because five skips
are OS-specific and cannot reproduce on Linux.

### Linux CI (10 after this branch)

| Test | Skip reason | Why it is correct |
|---|---|---|
| `tests/test_hf_app.py` (5 tests) | `could not import 'gradio'` | Gradio is only installed in the Hugging Face Space image, not in `requirements-dev.txt`; no CI job installs it. |
| `tests/test_hf_app.py` (4 tests) | `could not import 'spaces'` | `huggingface_hub`'s ZeroGPU `spaces` package only exists on the Space runtime. |
| `tests/test_hf_requirements.py::…` | `set ROUTTENER_CHECK_PYPI=1 (legacy ROUTELLM_CHECK_PYPI) to query PyPI` | Makes real network calls to PyPI. Opt-in so the suite stays offline and deterministic. |

The former `tests/unit/test_ml_classifier.py::test_corrupt_pickle_is_graceful`
skip (`could not import 'joblib'`) is **fixed**: `joblib>=1.3` is now a
development dependency. It is a small pure-Python package; sklearn and
sentence-transformers stay out of CI on purpose. The sibling test that
simulates joblib being absent still covers the fail-soft path.

### Windows-local only (5, never in Linux CI)

| Test | Skip reason |
|---|---|
| `tests/test_deploy_scripts.py` | `bash` is the WSL stub with no distro, so it cannot produce a syntax verdict; the check runs on Linux CI. |
| `tests/test_hf_app.py` (2 tests) | Windows `SO_REUSEADDR` lets a probe bind a port an active listener holds; the conflict contract targets the Linux deploy host. |
| `tests/test_run_launcher.py` | Same Windows `SO_REUSEADDR` behaviour for the occupied-port probe. |
| `tests/unit/test_local.py` | POSIX permission bits only. |

**New since PR #16?** None. Every skip site above (`importorskip` calls and
platform conditions) already existed at PR #16's merge commit `35898f9`;
that run also reported 11 skipped on Linux. The 15-vs-11 difference is the
five Windows-only skips minus the one joblib skip that this branch fixes.

No skip uses a blanket "flaky" condition. If a test ever needs
`skipif(sys.platform == "win32", reason=...)`, the reason string must name the
specific platform behaviour, never "flaky".

## Determinism

- The provider wire is mocked (`respx` / `httpx.MockTransport`); no test needs a
  real provider key or outbound network.
- Provider shapes and free-tier labels in the UI are **estimates**
  (`tier_source: "estimated"`). `ROUTTENER_CAPTURE_PROVIDER_SHAPES=true` writes
  sanitized field names and truncated, redacted samples to
  `data/provider_shapes/<provider>.json` so they can be replaced with observed
  data. That directory is gitignored and never contains a key.
- `docs/field-reality.md` records which provider facts are OBSERVED LIVE,
  DOCUMENTED or ASSUMED. Nothing in the UI should present an estimate as
  verified.

## Reading a failure

- `model_not_found` is never an auth problem. Only `auth_failed` mentions the
  key, and only 401 (or Google's literal "API key not valid") calls it invalid.
- A 403 on a key that could list models is restrictions/project/region, not a
  bad key. See the README troubleshooting table.
- Provider error bodies are kept to 1000 redacted characters precisely so a
  failure can be understood from the log alone.
