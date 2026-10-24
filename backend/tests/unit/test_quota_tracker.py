import pytest
from fakeredis import FakeAsyncRedis

from app.core import quota_tracker
from app.core.quota_tracker import (
    DAILY_REQUEST_LIMITS,
    DEFAULT_DAILY_REQUEST_LIMIT,
    QuotaTracker,
)


@pytest.fixture
async def redis_client():
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.flushall()


@pytest.fixture
def tracker(redis_client):
    return QuotaTracker(redis_client)


async def test_fresh_provider_is_within_quota(tracker):
    assert await tracker.is_within_quota("gemini") is True


async def test_reaching_daily_limit_blocks_provider(tracker, monkeypatch):
    monkeypatch.setitem(DAILY_REQUEST_LIMITS, "gemini", 3)
    for _ in range(2):
        assert await tracker.is_within_quota("gemini") is True
        await tracker.record_usage("gemini")

    await tracker.record_usage("gemini")  # count now reaches the limit
    assert await tracker.is_within_quota("gemini") is False


async def test_record_usage_increments_both_counters(tracker, redis_client):
    await tracker.record_usage("groq", tokens_used=150)
    await tracker.record_usage("groq", tokens_used=75)

    summary = await tracker.get_usage_summary("groq")
    assert summary["requests_today"] == 2
    assert summary["tokens_today"] == 225
    # Day-scoped keys clean themselves up.
    assert 0 < await redis_client.ttl(tracker._requests_key("groq")) <= 86400


async def test_record_usage_without_tokens_leaves_token_counter_at_zero(tracker):
    await tracker.record_usage("groq")
    summary = await tracker.get_usage_summary("groq")
    assert summary["requests_today"] == 1
    assert summary["tokens_today"] == 0


async def test_get_usage_summary_percentage(tracker, monkeypatch):
    monkeypatch.setitem(DAILY_REQUEST_LIMITS, "groq", 10)
    for _ in range(4):
        await tracker.record_usage("groq", tokens_used=100)

    summary = await tracker.get_usage_summary("groq")
    assert summary == {
        "requests_today": 4,
        "tokens_today": 400,
        "daily_limit": 10,
        "percentage_used": 40.0,
    }


async def test_unknown_provider_uses_fallback_limit(tracker):
    summary = await tracker.get_usage_summary("brand_new_provider")
    assert summary["daily_limit"] == DEFAULT_DAILY_REQUEST_LIMIT

    await tracker.record_usage("brand_new_provider")
    assert await tracker.is_within_quota("brand_new_provider") is True

    # Generic: fill the fallback limit and confirm blocking works for a
    # provider that has never been hardcoded anywhere.
    for _ in range(DEFAULT_DAILY_REQUEST_LIMIT):
        await tracker.record_usage("brand_new_provider")
    assert await tracker.is_within_quota("brand_new_provider") is False
