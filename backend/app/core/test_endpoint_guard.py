"""Server-side protection for endpoints that spend the user's provider quota.

Every "test" endpoint makes a REAL call to a provider (Groq/Gemini/DeepSeek)
with the caller's stored key. Without a guard, one browser tab (or one
malicious script) can burn an account's rate limit by hammering the button.

Two independent limiters, both in-process (the local-first deployment is one
process; a multi-worker deployment gets per-worker limits, which is still a
strict improvement over nothing and is honest about it in the docstring):

* a sliding-window rate limit — TEST_RATE_LIMIT per key per 60s window;
* a concurrency cap — TEST_MAX_IN_FLIGHT simultaneously running tests per
  key. A test beyond either limit answers 429 with a Retry-After header.

The UI must respect it (the "Test all" batches of 5 stay well below both).
"""

from __future__ import annotations

import time
from collections import deque

from fastapi import HTTPException

#: Maximum test calls per RouteLLM key per rolling 60 seconds.
TEST_RATE_LIMIT = 10

#: Window length in seconds for the rate limit.
TEST_RATE_WINDOW_S = 60.0

#: Maximum simultaneously running tests per RouteLLM key.
TEST_MAX_IN_FLIGHT = 5

_call_times: dict[int, deque[float]] = {}
_in_flight: dict[int, int] = {}


def check_and_acquire(key_id: int) -> None:
    """Rate-limit + acquire a slot, or raise 429 with Retry-After.

    Must be paired with exactly one :func:`release` in a finally block.
    """
    now = time.monotonic()

    times = _call_times.setdefault(key_id, deque())
    while times and (now - times[0]) >= TEST_RATE_WINDOW_S:
        times.popleft()
    if len(times) >= TEST_RATE_LIMIT:
        retry_after = int(TEST_RATE_WINDOW_S - (now - times[0])) + 1
        raise HTTPException(
            status_code=429,
            detail=(
                f"Rate limit: at most {TEST_RATE_LIMIT} tests per minute per key. "
                "Try again shortly."
            ),
            headers={"Retry-After": str(retry_after)},
        )

    if _in_flight.get(key_id, 0) >= TEST_MAX_IN_FLIGHT:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Too many tests already running for this key "
                f"(max {TEST_MAX_IN_FLIGHT} in flight). Try again shortly."
            ),
            headers={"Retry-After": "2"},
        )

    times.append(now)
    _in_flight[key_id] = _in_flight.get(key_id, 0) + 1


def release(key_id: int) -> None:
    """Give back a concurrency slot acquired by :func:`check_and_acquire`."""
    count = _in_flight.get(key_id, 0)
    if count <= 1:
        _in_flight.pop(key_id, None)
    else:
        _in_flight[key_id] = count - 1


def _reset() -> None:
    """Test hook: wipe both limiters."""
    _call_times.clear()
    _in_flight.clear()
