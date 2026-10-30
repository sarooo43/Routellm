"""ChaosController unit tests — Redis key semantics for fault injection."""

import pytest
from fakeredis import FakeAsyncRedis

from app.core.chaos import (
    DEFAULT_DURATION_SECONDS,
    ChaosController,
)

PROVIDER = "some_new_provider"  # generic name: no hardcoded provider matching


@pytest.fixture
async def redis_client():
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.flushall()


@pytest.fixture
def chaos(redis_client):
    return ChaosController(redis_client)


async def test_inject_sets_redis_key_with_ttl(chaos, redis_client):
    ttl = await chaos.inject_failure(PROVIDER, duration_seconds=90)

    assert ttl == 90
    key = f"chaos:{PROVIDER}:forced_failure"
    assert await redis_client.get(key) == "1"
    stored_ttl = await redis_client.ttl(key)
    assert 0 < stored_ttl <= 90


async def test_inject_uses_default_duration_when_omitted(chaos, redis_client):
    ttl = await chaos.inject_failure(PROVIDER)

    assert ttl == DEFAULT_DURATION_SECONDS
    stored_ttl = await redis_client.ttl(f"chaos:{PROVIDER}:forced_failure")
    assert 0 < stored_ttl <= DEFAULT_DURATION_SECONDS


async def test_inject_clamps_bogus_durations(chaos):
    assert (await chaos.inject_failure(PROVIDER, 0)) >= 1
    assert (await chaos.inject_failure(PROVIDER, -10)) >= 1


async def test_is_chaos_active_true_immediately_after_injection(chaos):
    await chaos.inject_failure(PROVIDER, duration_seconds=60)
    assert await chaos.is_chaos_active(PROVIDER) is True


async def test_is_chaos_active_false_after_clear(chaos):
    await chaos.inject_failure(PROVIDER, duration_seconds=60)
    await chaos.clear_failure(PROVIDER)
    assert await chaos.is_chaos_active(PROVIDER) is False


async def test_is_chaos_active_false_for_never_injected_provider(chaos):
    assert await chaos.is_chaos_active("never_touched_provider") is False


async def test_clear_is_idempotent(chaos):
    await chaos.clear_failure(PROVIDER)  # never injected — must not raise
    assert await chaos.is_chaos_active(PROVIDER) is False


async def test_get_all_active_chaos_lists_multiple_injected_providers(chaos):
    assert await chaos.get_all_active_chaos() == []

    await chaos.inject_failure("beta", duration_seconds=60)
    await chaos.inject_failure("alpha", duration_seconds=60)
    await chaos.inject_failure("gamma", duration_seconds=60)
    # Clearing one must remove it from the listing.
    await chaos.clear_failure("gamma")

    assert await chaos.get_all_active_chaos() == ["alpha", "beta"]


async def test_get_all_active_chaos_survives_exotic_provider_names(chaos):
    # Provider names containing colons still parse from the flat key endings.
    await chaos.inject_failure("weird:provider", duration_seconds=60)
    assert await chaos.get_all_active_chaos() == ["weird:provider"]
