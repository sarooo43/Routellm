"""Fallback executor — the routing brain that wires everything together.

Flow: classify → snapshot REAL stats (Postgres history + live Redis circuit
state) → rank → attempt in order (gated by quota / circuit checks) → record
telemetry per attempt → persist TaskLog / ModelStats / UsageTracking →
fall through on failure. Never raises: provider, Redis, and DB failures all
degrade into structured, routable behavior instead of exceptions.
"""

import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as redis_async
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.circuit_breaker import CircuitBreaker
from app.core.chaos import ChaosController
from app.core.classifier import classify_task
from app.core.quota_tracker import QuotaTracker
from app.core.scorer import ModelStats, rank_models
from app.models import ModelStatsDB, TaskLog, UsageTracking
from app.providers.base import ProviderResponse
from app.providers.registry import PROVIDER_REGISTRY
from app.utils.logger import get_logger

logger = get_logger(__name__)


class AttemptLog(BaseModel):
    provider_name: str
    success: bool
    error: Optional[str] = None
    latency_ms: float = 0.0
    # True for synthesized chaos failures (DEV/DEMO ONLY) — lets the
    # dashboard paint them purple instead of "real failure" red.
    chaos_injected: bool = False


class ExecutionResult(BaseModel):
    success: bool
    result_text: str = ""
    model_used: Optional[str] = None
    confidence_score: float = 0.0
    fallback_chain_attempted: list[AttemptLog] = Field(default_factory=list)
    total_latency_ms: float = 0.0
    tokens_used: int = 0
    error: Optional[str] = None  # populated only if ALL providers failed
    request_id: str = ""


# --------------------------------------------------------------------------
# Shared circuit-breaker / quota-tracker instances (Redis-backed).
# Injectable for tests; lazily created for production use.
# --------------------------------------------------------------------------

_circuit_breaker: Optional[CircuitBreaker] = None
_quota_tracker: Optional[QuotaTracker] = None
_chaos_controller: Optional[ChaosController] = None


def _get_redis_client():
    if settings.REDIS_URL:
        return redis_async.from_url(settings.REDIS_URL, decode_responses=True)
    logger.warning(
        "redis_unavailable_fallback",
        detail="REDIS_URL not set; using in-memory fakeredis. State does not "
        "survive restarts and is NOT shared across instances (dev only).",
    )
    from fakeredis import FakeAsyncRedis

    return FakeAsyncRedis(decode_responses=True)


def _ensure_shared_clients() -> tuple[CircuitBreaker, QuotaTracker, ChaosController]:
    global _circuit_breaker, _quota_tracker, _chaos_controller
    if _circuit_breaker is None:
        client = _get_redis_client()
        _circuit_breaker = CircuitBreaker(client)
        _quota_tracker = QuotaTracker(client)
        # Chaos keys are inert unless settings.CHAOS_MODE_ENABLED is true:
        # executor + routes both gate on the flag, never on key existence.
        _chaos_controller = ChaosController(client)
    return _circuit_breaker, _quota_tracker, _chaos_controller


# --------------------------------------------------------------------------
# Stats snapshot — REAL historical stats from the DB, circuit state from Redis
# (circuit state is real-time ephemeral; success/latency history is permanent).
# --------------------------------------------------------------------------


async def _build_stats_snapshot(
    task_type: str,
    circuit_breaker: CircuitBreaker,
    db_session: AsyncSession,
) -> dict[str, ModelStats]:
    snapshot: dict[str, ModelStats] = {}

    db_rows: dict[str, ModelStatsDB] = {}
    if db_session is not None:
        try:
            stmt = select(ModelStatsDB).where(ModelStatsDB.task_type == task_type)
            rows = (await db_session.execute(stmt)).scalars().all()
            db_rows = {row.provider_name: row for row in rows}
        except Exception as e:
            logger.warning(
                "stats_query_failed_using_optimistic_defaults",
                task_type=task_type,
                error=str(e),
            )

    for provider_name in PROVIDER_REGISTRY:
        try:
            state = await circuit_breaker.get_state(provider_name)
        except Exception as e:
            logger.warning(
                "circuit_state_unavailable_treating_closed",
                provider=provider_name,
                error=str(e),
            )
            state = "CLOSED"

        row = db_rows.get(provider_name)
        if row is not None:
            snapshot[provider_name] = ModelStats(
                provider_name=provider_name,
                task_type=task_type,
                total_requests=row.total_requests,
                successful_requests=row.successful_requests,
                avg_latency_ms=row.avg_latency_ms,
                circuit_state=state,
            )
        else:
            # No history yet for this provider/task pair: optimistic defaults
            # (total_requests=0 → scorer uses 0.75 success rate).
            snapshot[provider_name] = ModelStats(
                provider_name=provider_name,
                task_type=task_type,
                total_requests=0,
                successful_requests=0,
                circuit_state=state,
            )
    return snapshot


# --------------------------------------------------------------------------
# Persistence (best-effort: a DB outage must never break routing)
# --------------------------------------------------------------------------


async def _upsert_model_stats(
    db_session: AsyncSession,
    task_type: str,
    provider_name: str,
    success: bool,
    latency_ms: float,
) -> None:
    stmt = select(ModelStatsDB).where(
        ModelStatsDB.provider_name == provider_name,
        ModelStatsDB.task_type == task_type,
    )
    row = (await db_session.execute(stmt)).scalars().first()
    if row is None:
        row = ModelStatsDB(provider_name=provider_name, task_type=task_type)
        db_session.add(row)

    row.total_requests += 1
    if success:
        row.successful_requests += 1
    row.success_rate = row.successful_requests / row.total_requests
    # Running average: ((old_avg * (n - 1)) + this_latency) / n
    row.avg_latency_ms = (
        row.avg_latency_ms * (row.total_requests - 1) + latency_ms
    ) / row.total_requests
    row.last_updated = datetime.now(timezone.utc)


async def _upsert_usage_tracking(
    db_session: AsyncSession,
    provider_name: str,
    tokens_used: int,
) -> None:
    today = datetime.now(timezone.utc).date()
    stmt = select(UsageTracking).where(
        UsageTracking.provider_name == provider_name,
        UsageTracking.date == today,
    )
    row = (await db_session.execute(stmt)).scalars().first()
    if row is None:
        row = UsageTracking(provider_name=provider_name, date=today)
        db_session.add(row)
    row.requests_count += 1
    row.tokens_used += tokens_used


async def _persist_result(
    db_session: AsyncSession,
    request_id: str,
    task_type: str,
    attempt_records: list[dict],
    result: ExecutionResult,
) -> None:
    """Write TaskLog + per-provider stats/usage rows, committed once."""
    if db_session is None:
        return
    try:
        db_session.add(
            TaskLog(
                request_id=request_id,
                task_type=task_type,
                selected_model=result.model_used,
                confidence_score=result.confidence_score,
                status="success" if result.success else "failed",
                latency_ms=result.total_latency_ms,
                tokens_used=result.tokens_used,
                error_message=result.error,
                fallback_chain=[
                    attempt.model_dump()
                    for attempt in result.fallback_chain_attempted
                ],
            )
        )
        for record in attempt_records:
            await _upsert_model_stats(
                db_session,
                task_type=task_type,
                provider_name=record["provider_name"],
                success=record["success"],
                latency_ms=record["latency_ms"],
            )
            await _upsert_usage_tracking(
                db_session,
                record["provider_name"],
                record["tokens_used"],
            )
        await db_session.commit()
    except Exception as e:
        await db_session.rollback()
        logger.error(
            "db_logging_failed",
            request_id=request_id,
            error=str(e),
        )


# --------------------------------------------------------------------------
# Defensive wrappers: if Redis is down, prefer attempting over dead-gating.
# --------------------------------------------------------------------------


async def _quota_allows(quota_tracker: QuotaTracker, provider_name: str) -> bool:
    try:
        return await quota_tracker.is_within_quota(provider_name)
    except Exception as e:
        logger.warning(
            "quota_check_failed_failing_open", provider=provider_name, error=str(e)
        )
        return True


async def _circuit_state(circuit_breaker: CircuitBreaker, provider_name: str) -> str:
    try:
        return await circuit_breaker.get_state(provider_name)
    except Exception as e:
        logger.warning(
            "circuit_check_failed_failing_closed",
            provider=provider_name,
            error=str(e),
        )
        return "CLOSED"


async def _chaos_active(
    chaos_controller: Optional[ChaosController], provider_name: str
) -> bool:
    """DEV/DEMO ONLY check — always False with chaos mode off or Redis down."""
    if chaos_controller is None:
        return False
    try:
        return await chaos_controller.is_chaos_active(provider_name)
    except Exception as e:
        logger.warning(
            "chaos_check_failed_treating_inactive",
            provider=provider_name,
            error=str(e),
        )
        return False


def _deepseek_kwargs(task_type: str) -> dict:
    return {
        "model_variant": "deepseek-coder" if task_type == "code" else "deepseek-chat",
    }


# --------------------------------------------------------------------------
# Naive single-provider execution (benchmark baseline)
# --------------------------------------------------------------------------


async def _execute_forced_single_provider(
    provider_name: str,
    prompt: str,
    db_session: AsyncSession,
    resolved_task_type: str,
    request_id: str,
    start: float,
    chaos: Optional[ChaosController],
) -> ExecutionResult:
    """The naive alternative RouteLLM is benchmarked against: one hardcoded
    model, zero resilience infrastructure — no scoring/ranking, no quota or
    circuit pre-flight gates, no retry/fallback, and (deliberately) no
    breaker/quota side effects since a naive deployment has neither.

    Chaos injections STILL apply: an outage is a property of the provider,
    so the simulation must hit this path identically to the full system.
    """
    if provider_name not in PROVIDER_REGISTRY:
        raise ValueError(
            f"unknown force_provider '{provider_name}' "
            f"(known: {sorted(PROVIDER_REGISTRY)})"
        )

    kwargs = _deepseek_kwargs(resolved_task_type) if provider_name == "deepseek" else {}

    # ---- Chaos gate (DEV/DEMO ONLY): same simulated outage as the full path.
    chaos_injected = False
    if settings.CHAOS_MODE_ENABLED and await _chaos_active(chaos, provider_name):
        logger.warning(
            "chaos_failure_injected_forced_provider",
            request_id=request_id,
            provider=provider_name,
            task_type=resolved_task_type,
        )
        response = ProviderResponse(
            success=False,
            text="",
            tokens_used=0,
            latency_ms=50.0,
            error="chaos_injected_failure",
            provider_name=provider_name,
            model_name="n/a",
        )
        chaos_injected = True
    else:
        provider = PROVIDER_REGISTRY[provider_name]
        try:
            response = await provider.generate(prompt, **kwargs)
        except Exception as e:
            # Naive deployments have no error net at all: a raised exception
            # would crash the caller. This catch ONLY converts it into the
            # structured response shape so the benchmark can record it.
            logger.error(
                "forced_provider_raised_unexpectedly",
                request_id=request_id,
                provider=provider_name,
                error=str(e),
            )
            response = ProviderResponse(
                success=False,
                text="",
                tokens_used=0,
                latency_ms=0.0,
                error=f"unexpected_error: {e}",
                provider_name=provider_name,
                model_name="unknown",
            )

    result = ExecutionResult(
        success=response.success,
        result_text=response.text if response.success else "",
        model_used=provider_name if response.success else None,
        confidence_score=0.0,  # no ranking happened — no confidence to report
        fallback_chain_attempted=[
            AttemptLog(
                provider_name=provider_name,
                success=response.success,
                error=response.error,
                latency_ms=response.latency_ms,
                chaos_injected=chaos_injected,
            )
        ],
        total_latency_ms=(time.perf_counter() - start) * 1000.0,
        tokens_used=response.tokens_used,
        error=None if response.success else response.error,
        request_id=request_id,
    )
    await _persist_result(
        db_session, request_id, resolved_task_type, [], result
    )
    if response.success:
        logger.info(
            "execution_succeeded_forced",
            request_id=request_id,
            provider=provider_name,
            task_type=resolved_task_type,
        )
    else:
        logger.error(
            "forced_provider_failed_no_fallback",
            request_id=request_id,
            provider=provider_name,
            error=response.error,
        )
    return result


# --------------------------------------------------------------------------
# Main orchestration
# --------------------------------------------------------------------------


async def execute_with_fallback(
    prompt: str,
    db_session: AsyncSession,
    task_type: Optional[str] = None,
    max_fallbacks: int = 3,
    circuit_breaker: Optional[CircuitBreaker] = None,
    quota_tracker: Optional[QuotaTracker] = None,
    weights: Optional[dict] = None,
    chaos_controller: Optional[ChaosController] = None,
    force_provider: Optional[str] = None,
) -> ExecutionResult:
    """Execute a prompt across ranked providers with automatic fallback.

    db_session is REQUIRED (used for the stats snapshot and all
    logging writes). Skipped providers (quota/circuit) are logged in the
    chain but do not consume the max_fallbacks budget — only real provider
    calls count as attempts.

    force_provider (benchmarks only): skips scoring/ranking and pre-flight
    gating entirely; attempts ONLY that provider with zero fallback — the
    naive "hardcoded single model, no resilience system" baseline.
    """
    request_id = str(uuid.uuid4())
    start = time.perf_counter()

    breaker = circuit_breaker if circuit_breaker is not None else _ensure_shared_clients()[0]
    quota = quota_tracker if quota_tracker is not None else _ensure_shared_clients()[1]
    chaos = (
        chaos_controller if chaos_controller is not None else _ensure_shared_clients()[2]
    )

    resolved_task_type = task_type if task_type else classify_task(prompt or "")

    # ---- Benchmark baseline branch: naive single-provider execution ----
    if force_provider is not None:
        return await _execute_forced_single_provider(
            force_provider,
            prompt,
            db_session,
            resolved_task_type=resolved_task_type,
            request_id=request_id,
            start=start,
            chaos=chaos,
        )

    max_attempts = max(1, max_fallbacks)

    stats_snapshot = await _build_stats_snapshot(resolved_task_type, breaker, db_session)
    ranked = rank_models(
        resolved_task_type,
        list(PROVIDER_REGISTRY.keys()),
        stats_snapshot,
        weights,
    )
    confidence_by_provider = {r.provider_name: r.confidence_score for r in ranked}

    attempts: list[AttemptLog] = []
    attempt_records: list[dict] = []  # real attempts only: for DB stats updates
    real_attempts = 0

    for ranked_model in ranked:
        if real_attempts >= max_attempts:
            break

        provider_name = ranked_model.provider_name

        # ---- Pre-flight gate 1: quota (free to check, no call wasted) ----
        if not await _quota_allows(quota, provider_name):
            attempts.append(
                AttemptLog(
                    provider_name=provider_name,
                    success=False,
                    error="quota_exceeded",
                    latency_ms=0.0,
                )
            )
            logger.info(
                "provider_skipped_quota",
                request_id=request_id,
                provider=provider_name,
                task_type=resolved_task_type,
            )
            continue

        # ---- Pre-flight gate 2: circuit breaker ----
        if await _circuit_state(breaker, provider_name) == "OPEN":
            attempts.append(
                AttemptLog(
                    provider_name=provider_name,
                    success=False,
                    error="circuit_open",
                    latency_ms=0.0,
                )
            )
            logger.info(
                "provider_skipped_circuit_open",
                request_id=request_id,
                provider=provider_name,
                task_type=resolved_task_type,
            )
            continue

        # ---- Pre-flight gate 3: chaos injection (DEV/DEMO ONLY) ----
        # A forced failure is NOT a skip: it flows through the same attempt /
        # telemetry path as a real failed call, so the breaker can trip from
        # repeated injections and the dashboard shows the purple chain step.
        chaos_injected = False
        if settings.CHAOS_MODE_ENABLED and await _chaos_active(chaos, provider_name):
            logger.warning(
                "chaos_failure_injected",
                request_id=request_id,
                provider=provider_name,
                task_type=resolved_task_type,
            )
            response = ProviderResponse(
                success=False,
                text="",
                tokens_used=0,
                latency_ms=50.0,
                error="chaos_injected_failure",
                provider_name=provider_name,
                model_name="n/a",
            )
            chaos_injected = True
        else:
            # ---- Real attempt ----
            provider = PROVIDER_REGISTRY[provider_name]
            kwargs = _deepseek_kwargs(resolved_task_type) if provider_name == "deepseek" else {}

            try:
                response = await provider.generate(prompt, **kwargs)
            except Exception as e:
                # Providers promise never to raise; this is the last-resort net.
                logger.error(
                    "provider_raised_unexpectedly",
                    request_id=request_id,
                    provider=provider_name,
                    error=str(e),
                )
                response = ProviderResponse(
                    success=False,
                    text="",
                    tokens_used=0,
                    latency_ms=0.0,
                    error=f"unexpected_error: {e}",
                    provider_name=provider_name,
                    model_name="unknown",
                )

        attempts.append(
            AttemptLog(
                provider_name=provider_name,
                success=response.success,
                error=response.error,
                latency_ms=response.latency_ms,
                chaos_injected=chaos_injected,
            )
        )
        # Chaos attempts consume the fallback budget but never touch the
        # provider's permanent history — no API call happened, so ModelStats,
        # UsageTracking and quota stay honest.
        if not chaos_injected:
            attempt_records.append(
                {
                    "provider_name": provider_name,
                    "success": response.success,
                    "latency_ms": response.latency_ms,
                    "tokens_used": response.tokens_used,
                }
            )
        real_attempts += 1

        # ---- Post-attempt telemetry (must not break routing) ----
        try:
            if response.success:
                await breaker.record_success(provider_name)
            else:
                # Chaos failures count too: repeated injections SHOULD be able
                # to trip the real breaker — realistic and demonstrable.
                await breaker.record_failure(provider_name)
        except Exception as e:
            logger.warning(
                "circuit_breaker_update_failed",
                request_id=request_id,
                provider=provider_name,
                error=str(e),
            )
        if not chaos_injected:
            try:
                # Failed calls still count: a real 500/timeout often consumed quota.
                await quota.record_usage(provider_name, response.tokens_used)
            except Exception as e:
                logger.warning(
                    "quota_update_failed",
                    request_id=request_id,
                    provider=provider_name,
                    error=str(e),
                )

        if response.success:
            logger.info(
                "execution_succeeded",
                request_id=request_id,
                provider=provider_name,
                task_type=resolved_task_type,
                attempts=len(attempts),
                fallbacks_used=real_attempts,
            )
            result = ExecutionResult(
                success=True,
                result_text=response.text,
                model_used=provider_name,
                confidence_score=confidence_by_provider.get(provider_name, 0.0),
                fallback_chain_attempted=attempts,
                total_latency_ms=(time.perf_counter() - start) * 1000.0,
                tokens_used=response.tokens_used,
                request_id=request_id,
            )
            await _persist_result(
                db_session, request_id, resolved_task_type, attempt_records, result
            )
            return result

        logger.info(
            "provider_attempt_failed_continuing",
            request_id=request_id,
            provider=provider_name,
            error=response.error,
            latency_ms=response.latency_ms,
        )

    logger.error(
        "all_providers_failed",
        request_id=request_id,
        task_type=resolved_task_type,
        attempts=len(attempts),
        real_attempts=real_attempts,
    )
    result = ExecutionResult(
        success=False,
        error="all_providers_failed",
        fallback_chain_attempted=attempts,
        total_latency_ms=(time.perf_counter() - start) * 1000.0,
        request_id=request_id,
    )
    await _persist_result(
        db_session, request_id, resolved_task_type, attempt_records, result
    )
    return result
