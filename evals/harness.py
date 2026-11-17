"""Shared harness for the eval suite (E1-E5).

Everything here is deterministic and dependency-free: no network, no real
provider credentials, no Redis server, no real database. The pieces:

* :class:`MemoryRedis` - the tiny async subset of Redis that the gateway's
  circuit breaker / quota tracker / chaos controller actually use, backed by
  a plain dict (in-process, per-eval-run).
* :class:`ScriptedProvider` - a deterministic fake provider that answers from
  a per-request behavior function, so each resilience scenario is exact and
  repeatable in CI.
* :func:`build_eval_stack` - a fresh in-memory SQLite database plus isolated
  breaker/quota/chaos instances, so eval runs never touch real state.
* :func:`wilson_interval` - the Wilson score interval (95% by default) used
  for every reported success rate.
* :func:`percentile` - p50/p95 helper for the overhead eval.

The eval scripts drive the REAL routing code (``app.core.executor.execute_
with_fallback``) against these fakes - the only thing mocked is the provider
transport, never the router.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
import time
from typing import Callable, Optional

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.core.circuit_breaker import CircuitBreaker
from app.core.chaos import ChaosController
from app.core.quota_tracker import QuotaTracker
from app.providers.base import ModelProvider, ProviderResponse


# ---------------------------------------------------------------------------
# Deterministic in-process Redis subset
# ---------------------------------------------------------------------------


class MemoryRedis:
    """The async Redis operations the gateway's controllers use, in a dict.

    Supports get/set(+ex)/delete/incr/incrby/decr/expire/keys with per-key
    TTLs. Deterministic: no eviction, no timing races, nothing shared between
    runs (create a fresh instance per eval run).
    """

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._expiry: dict[str, float] = {}

    def _alive(self, key: str) -> bool:
        deadline = self._expiry.get(key)
        if deadline is not None and time.monotonic() >= deadline:
            self._data.pop(key, None)
            self._expiry.pop(key, None)
            return False
        return key in self._data

    async def get(self, key: str):
        return self._data.get(key) if self._alive(key) else None

    async def set(self, key: str, value, ex: Optional[int] = None):
        self._data[key] = str(value)
        if ex is not None:
            self._expiry[key] = time.monotonic() + ex
        return True

    async def delete(self, *keys: str):
        n = 0
        for key in keys:
            if key in self._data:
                self._data.pop(key, None)
                self._expiry.pop(key, None)
                n += 1
        return n

    async def incr(self, key: str):
        current = int(self._data.get(key, "0")) if self._alive(key) else 0
        self._data[key] = str(current + 1)
        return current + 1

    async def incrby(self, key: str, amount: int):
        current = int(self._data.get(key, "0")) if self._alive(key) else 0
        self._data[key] = str(current + amount)
        return current + amount

    async def decr(self, key: str):
        current = int(self._data.get(key, "0")) if self._alive(key) else 0
        self._data[key] = str(current - 1)
        return current - 1

    async def expire(self, key: str, seconds: int):
        if self._alive(key):
            self._expiry[key] = time.monotonic() + seconds
            return True
        return False

    async def keys(self, pattern: str):
        # The chaos controller is the only keys() caller, with a literal
        # prefix + "*" pattern.
        import fnmatch

        return [k for k in list(self._data) if self._alive(k) and fnmatch.fnmatch(k, pattern)]

    async def flushall(self):
        self._data.clear()
        self._expiry.clear()
        return True


# ---------------------------------------------------------------------------
# Scripted provider: deterministic responses, no network
# ---------------------------------------------------------------------------


class ScriptedProvider(ModelProvider):
    """Answers from a caller-supplied behavior function - fully deterministic.

    ``behavior(prompt, call_index)`` returns a ProviderResponse. Keeping the
    behavior as data (a function) makes each resilience scenario explicit and
    repeatable: the same eval run always produces the same numbers.
    """

    def __init__(
        self,
        provider_name: str,
        model_name: str,
        behavior: Callable[[str, int], ProviderResponse],
    ):
        self.provider_name = provider_name
        self.model_name = model_name
        self._behavior = behavior
        self.calls = 0

    async def generate(self, prompt: str, **kwargs) -> ProviderResponse:
        response = self._behavior(prompt, self.calls)
        self.calls += 1
        # Sleep for the scripted latency for real: without this, a "2500ms
        # slow response" returns in ~0ms of wall-clock time and the latency
        # columns would measure nothing but harness overhead.
        if response.latency_ms > 0:
            await asyncio.sleep(response.latency_ms / 1000.0)
        return response


def make_scripted_response(
    provider_name: str,
    model_name: str,
    *,
    success: bool = True,
    text: str = "scripted answer",
    latency_ms: float = 30.0,
    error: Optional[str] = None,
    outcome: Optional[str] = None,
    http_status: Optional[int] = None,
    retry_after: Optional[float] = None,
    tokens_used: int = 10,
) -> ProviderResponse:
    """Build one deterministic ProviderResponse (mirrors providers/base.make_error)."""
    return ProviderResponse(
        success=success,
        text=text if success else "",
        tokens_used=tokens_used if success else 0,
        latency_ms=latency_ms,
        error=error,
        provider_name=provider_name,
        model_name=model_name,
        outcome=outcome,
        http_status=http_status,
        retry_after=retry_after,
    )


# ---------------------------------------------------------------------------
# In-process eval stack: fresh DB + isolated breaker/quota/chaos
# ---------------------------------------------------------------------------


def build_eval_stack():
    """A complete, isolated routing environment for one eval run.

    Returns (session_maker, breaker, quota, chaos). The database is an
    in-memory SQLite (StaticPool: one shared connection) with every table
    created, the breaker/quota/chaos run on a fresh MemoryRedis. Nothing
    here outlives the run and nothing touches the developer's real ./data
    directory.
    """
    import app.models  # noqa: F401 - registers every table on SQLModel.metadata

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    redis = MemoryRedis()

    async def _build():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        return (
            async_sessionmaker(engine, expire_on_commit=False),
            CircuitBreaker(redis),
            QuotaTracker(redis),
            ChaosController(redis),
        )

    return _build()


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

#: Two-sided z for the standard 95% Wilson score interval.
Z_95 = 1.959963984540054


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion - never returns a
    degenerate [0, 1] span for n > 0 and is well-behaved at the extremes."""
    if n <= 0:
        return (0.0, 0.0)
    p_hat = successes / n
    z2 = z * z
    denominator = 1 + z2 / n
    centre = (p_hat + z2 / (2 * n)) / denominator
    spread = z * math.sqrt((p_hat * (1 - p_hat) + z2 / (4 * n)) / denominator)
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already-sorted list."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil((pct / 100.0) * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def count_real_attempts(fallback_chain: list) -> int:
    """Real provider calls in an attempt chain (skips are not calls).

    Quota/circuit pre-flight rejections are logged as chain steps with
    ``quota_exceeded`` / ``circuit_open`` errors but no provider call
    happened - they must not inflate "mean attempts".
    """
    skip_errors = {"quota_exceeded", "circuit_open"}
    return sum(1 for a in fallback_chain if a.error not in skip_errors)


def silence_structlog() -> None:
    """Point the app's structlog output at the void.

    The app configures structlog to print JSON events to stdout (see
    app/utils/logger.py). The eval scenarios intentionally produce hundreds
    of expected failure events; without this the report drowns in them.
    """
    import os
    import structlog

    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(
            file=open(os.devnull, "w", encoding="utf-8")
        )
    )
    logging.getLogger().setLevel(logging.CRITICAL)
