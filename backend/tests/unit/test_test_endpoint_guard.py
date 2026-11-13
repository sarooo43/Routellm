"""The quota guard for endpoints that spend the user's provider quota.

Each test endpoint makes a REAL provider call with the caller's key, so the
guard (10/min sliding window + 5 in flight, both 429 with Retry-After) is the
server-side protection, not a frontend courtesy.
"""

import pytest
from fastapi import HTTPException

from app.core import test_endpoint_guard


@pytest.fixture(autouse=True)
def _clean():
    test_endpoint_guard._reset()
    yield
    test_endpoint_guard._reset()


def test_first_ten_calls_pass_then_429_with_retry_after():
    for _ in range(test_endpoint_guard.TEST_RATE_LIMIT):
        test_endpoint_guard.check_and_acquire(key_id=1)
        test_endpoint_guard.release(1)

    with pytest.raises(HTTPException) as exc_info:
        test_endpoint_guard.check_and_acquire(key_id=1)
    assert exc_info.value.status_code == 429
    assert "Retry-After" in (exc_info.value.headers or {})
    assert int(exc_info.value.headers["Retry-After"]) >= 1


def test_windows_are_per_key_not_global():
    """Key 2 is unaffected by key 1's spending (and vice versa)."""
    for _ in range(test_endpoint_guard.TEST_RATE_LIMIT):
        test_endpoint_guard.check_and_acquire(key_id=1)
        test_endpoint_guard.release(1)

    # A different RouteLLM key still has its full budget.
    test_endpoint_guard.check_and_acquire(key_id=2)
    test_endpoint_guard.release(2)


def test_concurrency_cap_returns_429_with_retry_after():
    for _ in range(test_endpoint_guard.TEST_MAX_IN_FLIGHT):
        test_endpoint_guard.check_and_acquire(key_id=1)  # slots held, not released

    with pytest.raises(HTTPException) as exc_info:
        test_endpoint_guard.check_and_acquire(key_id=1)
    assert exc_info.value.status_code == 429
    assert exc_info.value.headers is not None
    assert "Retry-After" in exc_info.value.headers


def test_release_frees_the_concurrency_slot():
    test_endpoint_guard.check_and_acquire(key_id=1)
    test_endpoint_guard.release(1)
    # The rate budget is spent once, but the slot is free again.
    test_endpoint_guard.check_and_acquire(key_id=1)
    test_endpoint_guard.release(1)


def test_rate_limit_is_a_sliding_window(monkeypatch):
    import app.core.test_endpoint_guard as guard

    for _ in range(guard.TEST_RATE_LIMIT):
        guard.check_and_acquire(key_id=1)
        guard.release(1)

    # An hour later the whole window has expired: budget is back.
    real_monotonic = guard.time.monotonic
    monkeypatch.setattr(
        guard.time, "monotonic", lambda: real_monotonic() + guard.TEST_RATE_WINDOW_S + 1
    )
    guard.check_and_acquire(key_id=1)
    guard.release(1)
