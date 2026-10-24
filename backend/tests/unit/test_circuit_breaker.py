import pytest
from fakeredis import FakeAsyncRedis

from app.core.circuit_breaker import (
    COOLDOWN_SECONDS,
    FAILURE_THRESHOLD,
    CircuitBreaker,
)

PROVIDER = "some_new_provider"  # generic name: no hardcoded provider matching


@pytest.fixture
async def redis_client():
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.flushall()


@pytest.fixture
def breaker(redis_client):
    return CircuitBreaker(redis_client)


async def trip_to_open(breaker, name=PROVIDER):
    for _ in range(FAILURE_THRESHOLD):
        await breaker.record_failure(name)


async def test_fresh_provider_starts_closed(breaker):
    assert await breaker.get_state(PROVIDER) == "CLOSED"


async def test_below_threshold_stays_closed(breaker):
    for _ in range(FAILURE_THRESHOLD - 1):
        await breaker.record_failure(PROVIDER)
    assert await breaker.get_state(PROVIDER) == "CLOSED"


async def test_threshold_trips_open_with_cooldown_ttl(breaker, redis_client):
    await trip_to_open(breaker)
    assert await breaker.get_state(PROVIDER) == "OPEN"
    ttl = await redis_client.ttl(f"circuit:{PROVIDER}:state")
    assert 0 < ttl <= COOLDOWN_SECONDS


async def test_success_while_closed_decays_failure_count(breaker, redis_client):
    failures_key = f"circuit:{PROVIDER}:failures"
    for _ in range(4):
        await breaker.record_failure(PROVIDER)
    assert int(await redis_client.get(failures_key)) == 4

    await breaker.record_success(PROVIDER)
    assert int(await redis_client.get(failures_key)) == 3

    # 3 + 1 new failure = 4 < threshold → still healthy; without the decay
    # this would have tripped the breaker.
    await breaker.record_failure(PROVIDER)
    assert await breaker.get_state(PROVIDER) == "CLOSED"

    await breaker.record_failure(PROVIDER)
    assert await breaker.get_state(PROVIDER) == "OPEN"


async def test_state_key_expiry_means_half_open_when_failures_persist(
    breaker, redis_client
):
    await trip_to_open(breaker)
    # Simulate natural cooldown expiry by deleting the state key.
    await redis_client.delete(f"circuit:{PROVIDER}:state")
    assert await breaker.get_state(PROVIDER) == "HALF_OPEN"


async def test_half_open_success_transitions_to_closed(breaker, redis_client):
    await trip_to_open(breaker)
    await redis_client.delete(f"circuit:{PROVIDER}:state")
    assert await breaker.get_state(PROVIDER) == "HALF_OPEN"

    await breaker.record_success(PROVIDER)
    assert await breaker.get_state(PROVIDER) == "CLOSED"
    # Failure history is cleared after a successful probe.
    assert await redis_client.get(f"circuit:{PROVIDER}:failures") is None


async def test_half_open_failure_transitions_back_to_open(breaker, redis_client):
    await trip_to_open(breaker)
    await redis_client.delete(f"circuit:{PROVIDER}:state")
    assert await breaker.get_state(PROVIDER) == "HALF_OPEN"

    await breaker.record_failure(PROVIDER)
    assert await breaker.get_state(PROVIDER) == "OPEN"
    # Cooldown timer was re-armed fresh, not expired.
    ttl = await redis_client.ttl(f"circuit:{PROVIDER}:state")
    assert 0 < ttl <= COOLDOWN_SECONDS


async def test_failure_while_open_does_not_reset_cooldown(breaker, redis_client):
    for _ in range(2):
        await breaker.record_failure(PROVIDER)
        state = await breaker.get_state(PROVIDER)
        if state == "OPEN":
            break
    ttl_before = await redis_client.ttl(f"circuit:{PROVIDER}:state")
    await breaker.record_failure(PROVIDER)
    ttl_after = await redis_client.ttl(f"circuit:{PROVIDER}:state")
    assert ttl_after == ttl_before


async def test_unknown_provider_is_automatically_closed(breaker):
    # Brand-new provider with zero Redis keys: must be CLOSED, never crash.
    assert await breaker.get_state("some_brand_new_provider") == "CLOSED"
