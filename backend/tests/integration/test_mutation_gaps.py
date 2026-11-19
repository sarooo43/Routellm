"""Phase 7a: behaviours that survived the mutation sweep.

Two different things came out of the sweep and they must not be conflated:

* REAL GAPS (tests below): chaos injection, quota exhaustion and key-scoped
  provider keys had no assertion that would notice them being broken.
* EQUIVALENT MUTANTS (documented, not "caught"): two survivors are unreachable
  redundant guards, so no test can catch them and none should claim to.
  - ``get_decrypted_provider_keys`` returns ``{}`` when ``api_key_id is None``
    BEFORE it calls ``list_provider_keys``, so ``api_key_id or 1`` at that call
    site is dead code.
  - ``generate_with_walking``'s ``if response.outcome in _KEY_WIDE_OUTCOMES:
    return response, [], initial_model`` is repeated by the very next branch
    (``walkable`` is False for a key-wide outcome, and the ``not walkable``
    return is identical). Removing the first guard changes no observable
    behaviour.
  Both are defence in depth and are kept: the cost is two lines, and they make
  the intent readable next to the walk they prevent.
"""

import pytest
from fakeredis import FakeAsyncRedis

from app.core.api_key_manager import generate_api_key, get_local_identity
from app.core.provider_key_manager import (
    get_decrypted_provider_keys,
    upsert_provider_key,
)
from app.core.quota_tracker import QuotaTracker
from app.models.api_key import ApiKeyDB


# ---------------------------------------------------------------------------
# 1. Provider keys are scoped to the Routellm key that saved them.
#
# The `or 1` mutation at that call site is equivalent (see module docstring),
# but key scoping itself had no direct assertion. These pin it against a real
# row rather than a mocked one: a regression that dropped the id from the
# query would return another caller's credential.
# ---------------------------------------------------------------------------

async def test_a_null_identity_never_falls_back_to_another_callers_keys(db_session):
    owner = await get_local_identity(db_session)
    await upsert_provider_key(db_session, owner.id, "groq", "gsk_OWNERSECRET123456")

    # The fixture must have actually stored the key, or the assertion below is
    # vacuous.
    assert "groq" in await get_decrypted_provider_keys(db_session, owner.id)

    # A null identity must yield nothing; a fallback to id 1 (the local
    # identity in a fresh database) would hand its key over.
    assert await get_decrypted_provider_keys(db_session, None) == {}


async def test_one_callers_provider_keys_are_not_visible_to_another(db_session):
    first = await get_local_identity(db_session)
    raw, prefix, key_hash = generate_api_key()
    second = ApiKeyDB(key_hash=key_hash, key_prefix=prefix, label="second")
    db_session.add(second)
    await db_session.commit()
    await db_session.refresh(second)

    await upsert_provider_key(db_session, first.id, "groq", "gsk_FIRSTKEY1234567890")

    assert await get_decrypted_provider_keys(db_session, second.id) == {}
    assert "groq" in await get_decrypted_provider_keys(db_session, first.id)


# ---------------------------------------------------------------------------
# 2. A key problem stops the walk after ONE call.
#
# The guard itself is redundant (see module docstring), but the BEHAVIOUR it
# exists for - "one absent credential produces one attempt, never one per
# model" - had no assertion. A future refactor of the surrounding branches
# could easily lose it, so the behaviour is pinned, not the line.
# ---------------------------------------------------------------------------

async def test_a_missing_key_is_not_retried_once_per_model(monkeypatch):
    from app.core import model_discovery
    from app.providers.outcomes import error_for_missing_key

    calls: list[str] = []

    class _Provider:
        async def generate(self, prompt, **kwargs):
            calls.append(str(kwargs.get("model")))
            return error_for_missing_key(
                provider="groq", model=kwargs.get("model") or "default"
            )

    async def _many_models(*_a, **_k):
        from types import SimpleNamespace

        return [
            SimpleNamespace(id=f"model-{i}", display_name=f"model-{i}")
            for i in range(6)
        ], None

    monkeypatch.setattr(model_discovery, "discover_models", _many_models)
    monkeypatch.setattr(model_discovery, "operator_env_model", lambda _p: None)

    response, extra_steps, _initial = await model_discovery.generate_with_walking(
        "groq", "hi", {}, "gsk_FAKE0000000000", provider=_Provider()
    )

    assert response.outcome == "missing_provider_key"
    assert extra_steps == [], "a key-wide failure produced follow-up walk steps"
    assert len(calls) <= 1, f"one absent key produced {len(calls)} provider calls"


# ---------------------------------------------------------------------------
# 3. Chaos Mode replaces the provider call.
#
# SURVIVED: gating the injected response behind `if False` kept the suite
# green. Nothing asserted that chaos means "the provider is not called", which
# is the entire point of the feature.
# ---------------------------------------------------------------------------

async def test_a_chaos_injected_request_never_calls_the_provider(monkeypatch):
    from app.core import executor

    reached: list[int] = []

    class _Provider:
        async def generate(self, prompt, **kwargs):
            reached.append(1)
            raise AssertionError("the provider must not be called under chaos")

    async def _always_active(*_a, **_k):
        return True

    monkeypatch.setitem(executor.PROVIDER_REGISTRY, "groq", _Provider())
    monkeypatch.setattr(executor, "_chaos_active", _always_active)
    monkeypatch.setattr(executor.settings, "CHAOS_MODE_ENABLED", True, raising=False)

    result = await executor._execute_forced_single_provider(
        "groq",
        "hi",
        None,
        "general",
        "chaos-test",
        0.0,
        None,
        provider_api_keys={"groq": "gsk_FAKE0000000000"},
    )

    assert not reached, "the provider was called while chaos was active"
    assert result.success is False
    injected = [
        a
        for a in result.fallback_chain_attempted
        if getattr(a, "chaos_injected", False)
    ]
    assert injected, [a.model_dump() for a in result.fallback_chain_attempted]


# ---------------------------------------------------------------------------
# 4. An exhausted provider is out of quota.
#
# The sweep could not patch this one (the guard is a method call), so the
# behaviour is pinned directly rather than left to chance.
# ---------------------------------------------------------------------------

async def test_an_exhausted_provider_is_out_of_quota(monkeypatch):
    tracker = QuotaTracker(FakeAsyncRedis(decode_responses=True))
    monkeypatch.setattr(QuotaTracker, "_get_limit", staticmethod(lambda _name: 3))

    for _ in range(3):
        await tracker.record_usage("groq", tokens_used=1)

    assert await tracker.is_within_quota("groq") is False
    assert await tracker.is_within_quota("gemini") is True
