# Observation 5: `force_provider=mock` returned `missing_provider_key`

Reproduced and explained. This is the evidence trail, not a recollection.

## What the owner saw

> POST /api/v1/route with `force_provider=mock` and no keys logged
> `forced_provider_failed_no_fallback error=missing_provider_key`.

## Reproduction

Pristine `git clone --depth 1 --branch main` at `136fae0`, **all** `GROQ_*`,
`GEMINI_*`, `DEEPSEEK_*`, `DATABASE_URL`, `REDIS_URL`, `ENVIRONMENT` and `DEMO*`
variables deleted from the environment, then `execute_with_fallback` called
directly:

| Case | Result |
|---|---|
| `force_provider="mock"`, no keys at all | **success=True**, error=(none) |
| `force_provider="mock"`, BYOK map `{"gemini": "…"}` | **success=False, error=missing_provider_key** |

So the trigger is not "demo took a wrong path on a clean machine" — on a clean
machine mock works. It is **a caller who has saved a provider key**. The owner
had just saved a real Gemini key through the Providers page, which is exactly
that state.

## Why

In `app/core/executor.py`, the BYOK isolation gate refuses any provider the
caller did not configure:

```python
missing_byok = (
    bool(provider_api_keys)
    and provider_name not in provider_api_keys
    and provider_name != "mock"      # <- the fix
)
```

Before the fix that last clause did not exist, so a caller whose only key was
Gemini was told `missing_provider_key` when asking for the **synthetic demo
provider** — which reads no credential and makes no network call. The error
blames a key that is not involved, on a provider that cannot leak anything.

The "no keys at all" case never entered that branch (`provider_api_keys` was
empty), which is why a naive smoke test on a fresh clone passes and the owner's
real session failed. **The earlier smoke test was not wrong; it was
unrepresentative.** It tested the no-BYOK path only.

## The fix (already on this branch)

`provider_name != "mock"` is excluded from the gate, and demo mode being
deliberately off now returns a self-explaining message instead:

```
demo_disabled: Demo mode is off, enable it in Settings
(set DEMO_MODE_ENABLED=true, or delete the line in .env, then restart).
```

Never `missing_provider_key` — that string must only ever mean "a real
provider was called without a key".

Re-running the same matrix against this branch:

```
no keys at all                    success=True  error=(none)
BYOK: only gemini configured      success=True  error=(none)
```

## Regression tests

`backend/tests/integration/test_forced_mock_demo.py`:
- `test_forced_mock_succeeds_with_no_keys_at_all`
- `test_forced_mock_succeeds_even_with_a_stored_byok_key`  ← the owner's case
- `test_demo_disabled_gives_a_clear_message_never_missing_provider_key`
- `test_forced_real_provider_without_byok_still_reports_missing_key` (the gate
  must still work for real providers)
