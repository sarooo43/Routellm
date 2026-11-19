# The `provider_keys_routes.py` 65% coverage figure is a measurement artifact

**Status:** OPEN — needs an owner decision. Nothing is broken in the product;
the number is wrong.

## What the report says

`pytest --cov=app --cov-report=term-missing` (the exact CI command,
`.github/workflows/ci.yml:101`) prints:

```
app/api/provider_keys_routes.py     268     90    66%   106-107, 120, ..., 628-702
```

The striking part is the block `628-702`. That is the body of
`run_provider_diagnostics`, the in-app diagnostics endpoint.

## Why that block looked suspicious

Six tests in `backend/tests/integration/test_provider_diagnostics.py` pass, and
they assert on the endpoint's real response body:

```python
response = await client.post("/api/v1/provider-keys/gemini/diagnostics", headers=headers)
assert response.status_code == 200, response.text
assert body["list_count_before_filter"] == OBSERVED_LIVE_RAW_COUNT
```

A handler cannot return those numbers without its body executing. So either the
tests were lying (a fake hiding real behaviour — the failure mode this audit was
told to hunt) or the measurement was.

## The decisive experiment

Inserting `raise AssertionError("PROBE")` as the first statement of
`run_provider_diagnostics` makes
`test_diagnostics_runs_and_reports_every_required_field` **fail**. The body
therefore executes. The tests are honest; the report is not.

## Root cause

Coverage recorded **line 2287** as executed in a file that is **702 lines long**.

```
$ python -c "import coverage; d=coverage.CoverageData('.coverage'); d.read(); \
  print([l for l in d.lines('.../provider_keys_routes.py') if l > 702])"
[1317, 1727, 1736, 1790, 1981, 1991, 1993, 2000, 2149, 2151, 2154, 2287]
```

Line numbers that cannot exist in the file are being attributed to it.

**Proven mechanism.** Those numbers are real — they are lines of
`fastapi/routing.py`, which is 6552 lines long. The values land inside it:

```
fastapi/routing.py:2287   @asynccontextmanager
fastapi/routing.py:1317   methods = effective_context.methods
fastapi/routing.py:2149   if scope["method"] not in self.methods:
fastapi/routing.py:2000   def get_path(self, scope: Scope) -> str:
```

The module registers its routes with `@router.post(...)` /
`@router.put(...)`, and those decorator factories are `fastapi.routing`
functions that execute *while the module is being defined*, from
`provider_keys_routes.py`'s own frame stack. With the pure-Python tracer this
sandbox has (`hasattr(coverage.collector, 'CTracer')` is `False` — no C
extension), those frames are recorded against the file being defined instead of
the file whose code object they belong to.

**Minimal reproduction** (independent of this repository):

```python
# framework.py  - 3001 lines, a decorator factory defined after 3000 blanks
# appmod.py     - 5 lines, `from framework import route` + @route("/x")
lines recorded for appmod.py: [2, 4, 5, 6]     # 6 does not exist in a 5-line file
```

So the file's statement list is inflated by foreign lines: real statements get
reported "missing" and the percentage drops.

**Ruled out by experiment:** a plain `from helper import boom` does *not*
reproduce it (recorded `[2, 3, 4, 5]` for a 4-line file). It needs the foreign
callable to run as a **decorator** during definition — which is exactly what
this module does, and what ordinary imports do not.

## Things ruled out

| Hypothesis | Test | Result |
|---|---|---|
| Tests are mocked away | `raise` probe at the top of the handler | test fails → body really runs |
| Stale `.pyc` | compared pyc header mtime/size with source | identical ("pyc is current") |
| Module imported twice under two names | listed `sys.modules` entries | exactly one |
| Bytecode cache corruption | deleted every `__pycache__`, re-ran | same impossible lines |
| Report format bug (`term` vs `json`) | ran `coverage run` + `coverage report` directly | same impossible lines |
| Wrong file measured | printed `d.measured_files()` | exactly one path, the right one |

## Why this matters, and why it is not a code fix

* A 66% figure on a security-adjacent module will be read as "one third of
  provider-key handling is untested". That is misleading.
* It is **not** fixed here, because the fix is a testing-infrastructure choice:
  `pip install coverage[toml]`/building the C extension changes CI install time,
  and adding a `[tool.coverage]` omit/alias rule changes what the project
  reports. Both are the owner's call, and neither touches product behaviour.

## Owner options

1. **Install the C tracer** in CI (`pip install "coverage[c]"` or a wheel build).
   Most likely to produce correct numbers; adds a compile step where no wheel
   exists.
2. **Exclude the foreign frames**: `[tool.coverage.run] ` with an explicit
   `source` list plus `omit` for the vendored path — narrow, but the wrong line
   numbers would still be attached to the right file.
3. **Leave it and document it** (this file). Zero risk; the number stays wrong.

Recommendation: (1) if CI time allows, else (3). No product code should change
for this.
