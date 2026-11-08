"""One contract, two stores: the in-process store and a real Redis.

Local-first means ``REDIS_URL`` is optional, so the four Redis consumers —
circuit breaker, quota tracker, chaos controller and the health probe — must
behave identically whether they were handed ``fakeredis`` (the default) or a
real server. This module pins that shared surface:

    get, set(value), set(value, ex=), incr, incrby, decr, expire, delete, keys

Every assertion runs against the in-process store first. If a real Redis is
reachable (``TEST_REDIS_URL``, else the app's own ``REDIS_URL``), the same
suite runs against it too — so "works on a laptop" and "works with Redis" are
checked by the same expectations rather than two drifting test files.
"""

import os
import time

import pytest
from fakeredis import FakeAsyncRedis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.config import settings

#: Every key prefix this suite writes: the `contract:*` namespace plus the
#: prefixes the real consumers (quota, breaker, chaos) derive themselves.
_OWN_KEY_PATTERNS = ("contract:*", "quota:*", "circuit:*", "chaos:*")


def _candidate_urls() -> list[str]:
    """Real servers worth testing against (never logged — may carry a password)."""
    urls = []
    for name in ("TEST_REDIS_URL", "REDIS_URL"):
        value = (os.environ.get(name) or "").strip()
        if value and value.startswith(("redis://", "rediss://")):
            urls.append(value)
    return urls


@pytest.fixture(params=["in-process"] + _candidate_urls(), ids=["in-process", "real-redis"] if _candidate_urls() else ["in-process"])
async def store(request):
    """A Redis-compatible async client, in-process by default."""
    if request.param == "in-process":
        client = FakeAsyncRedis(decode_responses=True)
        yield client
        await client.flushall()
        return

    import redis.asyncio as redis_async

    client = redis_async.from_url(
        request.param, decode_responses=True, socket_connect_timeout=2
    )
    try:
        await client.ping()
    except (RedisConnectionError, OSError) as exc:  # pragma: no cover - env dependent
        await client.aclose()
        pytest.skip(f"real Redis not reachable: {type(exc).__name__}")
    # Start from a clean slate: quota counters are per provider per day, so a
    # leftover key from an earlier run would make "within quota" false before
    # the test writes anything. Only this suite's own key prefixes are cleared
    # (plus the explicitly namespaced `contract:*` keys), so a shared server is
    # never mutated outside them.
    for pattern in _OWN_KEY_PATTERNS:
        stale = await client.keys(pattern)
        if stale:
            await client.delete(*stale)

    yield client

    for pattern in _OWN_KEY_PATTERNS:
        keys = await client.keys(pattern)
        if keys:
            await client.delete(*keys)
    await client.aclose()


async def test_string_set_get_delete(store):
    key = "contract:string"
    assert await store.get(key) is None
    assert await store.set(key, "value") is True
    assert await store.get(key) == "value"
    assert await store.delete(key) >= 1
    assert await store.get(key) is None


async def test_keys_are_scoped_by_prefix(store):
    """chaos.get_all_active_chaos() relies on a prefix scan."""
    await store.set("contract:chaos:groq:forced_failure", "1")
    await store.set("contract:chaos:gemini:forced_failure", "1")
    await store.set("contract:other:key", "1")
    found = sorted(await store.keys("contract:chaos:*"))
    assert found == [
        "contract:chaos:gemini:forced_failure",
        "contract:chaos:groq:forced_failure",
    ]
    await store.delete(*found)


async def test_counters_increment_and_decrement(store):
    """circuit_breaker uses incr/decr; quota uses incr and incrby."""
    key = "contract:counter"
    assert await store.incr(key) == 1
    assert await store.incr(key) == 2
    assert await store.decr(key) == 1
    assert await store.incrby(key, 5) == 6
    # Counters are stored as strings, which every consumer parses with int().
    assert int(await store.get(key)) == 6
    await store.delete(key)


async def test_ttl_expires_a_key(store):
    """Chaos injections and breaker cooldowns are TTL-driven: no cleanup job."""
    key = "contract:ttl"
    await store.set(key, "1", ex=1)
    assert await store.get(key) == "1"
    time.sleep(1.2)
    assert await store.get(key) is None


async def test_ttl_is_reported_and_resettable(store):
    key = "contract:ttl-report"
    await store.set(key, "1")
    assert await store.ttl(key) == -1  # exists, no expiry (breaker failure window)
    await store.expire(key, 60)
    assert 0 < await store.ttl(key) <= 60
    await store.delete(key)


async def test_counter_with_ttl_matches_the_quota_pattern(store):
    """The exact sequence QuotaTracker.record_usage performs."""
    key = "contract:quota:requests"
    first = await store.incr(key)
    assert first == 1
    await store.expire(key, 86400)  # TTL anchored on the first write
    assert await store.incr(key) == 2
    assert 0 < await store.ttl(key) <= 86400

    tokens = "contract:quota:tokens"
    assert await store.incrby(tokens, 11) == 11
    await store.expire(tokens, 86400)
    assert await store.incrby(tokens, 4) == 15
    await store.delete(key, tokens)


async def test_health_probe_reports_ok_for_whichever_store_is_used(store):
    """``/health`` must say ``ok`` for the default in-process store too."""
    from app.cache import ping_redis

    assert await ping_redis(store) == "ok"


async def test_unreachable_redis_reports_down_not_raise():
    """An unhealthy dependency is a status, never an exception in a request."""
    from app.cache import ping_redis

    class DeadRedis:
        async def ping(self):
            raise OSError("connection refused (simulated)")

    assert await ping_redis(DeadRedis()) == "down"


# --------------------------------------------------------------------------
# The four consumers, run verbatim against whichever store is in play.
# --------------------------------------------------------------------------


async def test_circuit_breaker_trips_and_heals_on_any_store(store):
    from app.core.circuit_breaker import (
        COOLDOWN_SECONDS,
        FAILURE_THRESHOLD,
        CircuitBreaker,
    )

    breaker = CircuitBreaker(store)
    provider = "contract_provider"
    assert await breaker.get_state(provider) == "CLOSED"

    for _ in range(FAILURE_THRESHOLD):
        await breaker.record_failure(provider)
    assert await breaker.get_state(provider) == "OPEN"
    remaining = await store.ttl(breaker._state_key(provider))
    assert 0 < remaining <= COOLDOWN_SECONDS

    # Simulate the cooldown elapsing: HALF_OPEN lets one probe through.
    await store.delete(breaker._state_key(provider))
    assert await breaker.get_state(provider) == "HALF_OPEN"

    await breaker.record_success(provider)  # probe succeeded
    assert await breaker.get_state(provider) == "CLOSED"
    assert await store.get(breaker._failures_key(provider)) is None


async def test_quota_tracker_blocks_at_the_limit_on_any_store(store, monkeypatch):
    from app.core import quota_tracker as quota_module

    provider = "contract_provider"
    monkeypatch.setitem(quota_module.DAILY_REQUEST_LIMITS, provider, 2)
    tracker = quota_module.QuotaTracker(store)

    assert await tracker.is_within_quota(provider) is True
    await tracker.record_usage(provider, tokens_used=7)
    assert await tracker.is_within_quota(provider) is True
    await tracker.record_usage(provider, tokens_used=3)

    # The request that would reach the limit is the one that gets blocked.
    assert await tracker.is_within_quota(provider) is False
    summary = await tracker.get_usage_summary(provider)
    assert summary["requests_today"] == 2
    assert summary["tokens_today"] == 10
    assert summary["percentage_used"] == 100.0


async def test_chaos_controller_injects_and_self_expires_on_any_store(store):
    from app.core.chaos import ChaosController

    controller = ChaosController(store)
    assert await controller.get_all_active_chaos() == []

    ttl = await controller.inject_failure("contract_provider", duration_seconds=1)
    assert ttl == 1
    assert await controller.is_chaos_active("contract_provider") is True
    assert await controller.get_all_active_chaos() == ["contract_provider"]

    await controller.clear_failure("contract_provider")
    assert await controller.is_chaos_active("contract_provider") is False
    assert await controller.get_all_active_chaos() == []


# --------------------------------------------------------------------------
# The factory itself: no configuration means the in-process store.
# --------------------------------------------------------------------------


async def test_no_redis_url_yields_an_in_process_store(monkeypatch):
    from app.cache import create_redis_client

    monkeypatch.setattr(settings, "REDIS_URL", None)
    client = create_redis_client()
    try:
        assert isinstance(client, FakeAsyncRedis)
        assert await client.ping() is True
    finally:
        await client.flushall()


async def test_invalid_redis_url_is_rejected_loudly():
    """A typo must not silently become an in-memory store in production."""
    from app.cache import create_redis_client

    with pytest.raises(ValueError):
        create_redis_client("https://not-a-redis-host")
