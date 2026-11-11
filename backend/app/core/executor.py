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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.circuit_breaker import CircuitBreaker
from app.core.chaos import ChaosController
from app.core.classifier import classify_task
from app.core.ml_classifier import get_router_classifier
from app.core.model_discovery import (
    generate_with_walking,
    operator_env_key,
)
from app.core.quota_tracker import QuotaTracker
from app.core.scorer import ModelStats, rank_models
from app.models import Feedback, ModelStatsDB, TaskLog, UsageTracking
from app.providers.base import ProviderResponse
from app.providers.registry import PROVIDER_REGISTRY, routable_providers
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Outcomes that mean "the request was wrong", not "the provider is down".
#: These must never open the circuit breaker: a retired model name (404) is a
#: config bug on our side, and a 400 is a malformed request. Counting them as
#: outages would blacklist a perfectly healthy provider.
CONFIG_ERROR_OUTCOMES = frozenset(
    {"bad_request", "model_not_found", "endpoint_not_found", "not_found"}
)


class AttemptLog(BaseModel):
    provider_name: str
    success: bool
    error: Optional[str] = None
    latency_ms: float = 0.0
    # The concrete upstream model id this attempt (or skip) refers to, when
    # candidate walking selected one. None = legacy provider default.
    model: Optional[str] = None
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
    # EXPERIMENTAL transparency: which capability source ranked the
    # providers for this request ("ml_hybrid" | "rule_based").
    routing_method: str = "rule_based"


def _routing_method_in_effect() -> str:
    """EXPERIMENTAL visibility label. ml_hybrid only when the flag is on
    AND a trained model actually loaded; anything else is rule_based."""
    if settings.ENABLE_ML_ROUTING:
        classifier = get_router_classifier()
        if classifier.is_loaded:
            return "ml_hybrid"
    return "rule_based"


# --------------------------------------------------------------------------
# Shared circuit-breaker / quota-tracker instances (Redis-backed).
# Injectable for tests; lazily created for production use.
# --------------------------------------------------------------------------

_circuit_breaker: Optional[CircuitBreaker] = None
_quota_tracker: Optional[QuotaTracker] = None
_chaos_controller: Optional[ChaosController] = None


def _get_redis_client():
    """Shared Redis client (TLS-aware, short timeouts).

    Delegates to :mod:`app.cache` so quota tracking, the circuit breaker and
    /health all agree on one connection policy. A bad REDIS_URL must not take
    routing down: the failure is logged and an in-memory fakeredis client is
    used instead, which degrades exactly like "Redis not configured".
    """
    from app.cache import create_redis_client

    try:
        return create_redis_client()
    except Exception as e:
        logger.error(
            "redis_client_unavailable_using_memory",
            error=str(e).strip()[:300],
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

    # ---- Human feedback aggregate (thumbs up/down) per provider/task ----
    # Sparse by nature (most requests are never rated) but still a genuine
    # quality signal: populated onto ModelStats so the scorer's
    # feedback_adjusted_success_rate can blend it with the API success rate.
    feedback_counts: dict[tuple[str, str], int] = {}
    if db_session is not None:
        try:
            stmt = (
                select(Feedback.provider_name, Feedback.rating, func.count(Feedback.id))
                .where(Feedback.task_type == task_type)
                .group_by(Feedback.provider_name, Feedback.rating)
            )
            for provider_name, rating, count in (await db_session.execute(stmt)).all():
                feedback_counts[(provider_name, rating)] = int(count)
        except Exception as e:
            logger.warning(
                "feedback_query_failed_using_zero_defaults",
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
                positive_feedback_count=feedback_counts.get(
                    (provider_name, "positive"), 0
                ),
                negative_feedback_count=feedback_counts.get(
                    (provider_name, "negative"), 0
                ),
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
                positive_feedback_count=feedback_counts.get(
                    (provider_name, "positive"), 0
                ),
                negative_feedback_count=feedback_counts.get(
                    (provider_name, "negative"), 0
                ),
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
                routing_method=result.routing_method,
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


async def _invalidate_models(provider_name: str, api_key: Optional[str]) -> None:
    """Forget the cached model list so the next call asks the provider again."""
    try:
        from app.providers.model_registry import model_catalog

        # Every key for this provider: a retired model is retired for all of
        # them. Both tiers, or the stale list comes straight back from the DB.
        await model_catalog.invalidate_everywhere(provider_name)
        logger.info("model_cache_invalidated", provider=provider_name)
    except Exception as exc:  # pragma: no cover - cache is best-effort
        logger.warning("model_cache_invalidate_failed", provider=provider_name, error=str(exc))


def _deepseek_kwargs(task_type: str) -> dict:
    """Steer DeepSeek toward its code model **without hardcoding a model id**.

    This used to send ``model_variant="deepseek-coder"`` for every code task.
    That value reached the provider as an explicit model override, so discovery
    was bypassed for exactly the requests most likely to 404 — the retired-alias
    bug in one line. A preference flag cannot name a model that no longer
    exists, and the provider falls back to discovery when it is ignored.
    """
    return {"prefer_code_model": task_type == "code"}


def _generation_kwargs(
    provider_name: str,
    task_type: str,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> dict:
    """Forward explicit generation options without changing legacy defaults.

    Legacy /api/v1/route callers supply neither option, so their provider
    kwargs remain exactly as before. The OpenAI adapter uses this same executor
    path to honour temperature/max_tokens on both routed and forced calls.
    """
    kwargs = _deepseek_kwargs(task_type) if provider_name == "deepseek" else {}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    return kwargs


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
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    provider_api_keys: Optional[dict[str, str]] = None,
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

    kwargs = _generation_kwargs(provider_name, resolved_task_type, temperature, max_tokens)
    if provider_api_keys and provider_name in provider_api_keys:
        kwargs["provider_api_key"] = provider_api_keys[provider_name]

    # BYOK callers may only use providers they configured. A forced provider
    # without a caller key fails structurally without any upstream call.
    missing_byok = bool(provider_api_keys) and provider_name not in provider_api_keys

    # ---- Chaos gate (DEV/DEMO ONLY): same simulated outage as the full path.
    chaos_injected = False
    if missing_byok:
        response = ProviderResponse(
            success=False,
            text="",
            tokens_used=0,
            latency_ms=0.0,
            error="missing_provider_key",
            provider_name=provider_name,
            model_name="n/a",
        )
    elif settings.CHAOS_MODE_ENABLED and await _chaos_active(chaos, provider_name):
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
            if response.outcome == "model_not_found":
                # The model we chose is gone (or was never enabled). Invalidate
                # the discovery cache so the next request re-lists models
                # instead of repeating the same 404 for an hour.
                force_refresh = True
                await _invalidate_models(provider_name, kwargs.get("provider_api_key"))
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


def _operator_env_key(provider_name: str) -> Optional[str]:
    """Operator (non-BYOK) credential from settings, for candidate walking."""
    return operator_env_key(provider_name)


async def _execute_provider_attempt(
    provider_name: str,
    prompt: str,
    kwargs: dict,
    provider_api_key: Optional[str],
    pinned_model: Optional[str],
) -> tuple[ProviderResponse, list[AttemptLog], Optional[str]]:
    """One real provider attempt through the shared lazy candidate walk.

    With a credential available (BYOK caller key or operator .env key) the
    attempt goes through generate_with_walking(): the first attempt keeps
    legacy behavior (pinned/env/warm-cache/default model), and only a
    model_not_found triggers discovery + ranked candidate walking (max 3
    attempts). Without any credential the legacy single default-model call
    is kept so key-less demo behavior is unchanged.

    Returns (response, extra_attempt_logs, initial_model). extra_attempt_logs
    EXCLUDE the initial attempt (the caller logs that one itself).
    """
    if not provider_api_key:
        response = await PROVIDER_REGISTRY[provider_name].generate(prompt, **kwargs)
        return response, [], None
    response, extra_steps, initial_model = await generate_with_walking(
        provider_name,
        prompt,
        kwargs,
        provider_api_key,
        pinned_model=pinned_model,
        provider=PROVIDER_REGISTRY[provider_name],
    )
    logs = [
        AttemptLog(
            provider_name=provider_name,
            success=step.success,
            error=step.reason or step.error,
            latency_ms=step.latency_ms,
            model=step.model,
        )
        for step in extra_steps
    ]
    return response, logs, initial_model


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
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    provider_api_keys: Optional[dict[str, str]] = None,
    provider_models: Optional[dict[str, Optional[str]]] = None,
) -> ExecutionResult:
    """Execute a prompt across ranked providers with automatic fallback.

    db_session is REQUIRED (used for the stats snapshot and all
    logging writes). Skipped providers (quota/circuit) are logged in the
    chain but do not consume the max_fallbacks budget — only real provider
    calls count as attempts.

    force_provider (benchmarks only): skips scoring/ranking and pre-flight
    gating entirely; attempts ONLY that provider with zero fallback — the
    naive "hardcoded single model, no resilience system" baseline.

    temperature/max_tokens are optional pass-through generation settings;
    leaving them unset preserves each provider's existing defaults.

    provider_api_keys is the calling RouteLLM key's decrypted BYOK mapping.
    When non-empty, routing is restricted to those providers and each attempt
    receives its own ``provider_api_key`` kwarg. When empty/None, legacy
    operator/demo behavior is preserved (providers read settings.*_API_KEY).

    provider_models is the calling RouteLLM key's pinned model per provider
    (None = Auto). Routing precedence: pinned model, then the provider's
    .env model override, then Auto (candidate walking by rank).
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
            temperature=temperature,
            max_tokens=max_tokens,
            provider_api_keys=provider_api_keys,
        )

    max_attempts = max(1, max_fallbacks)

    routing_method = _routing_method_in_effect()

    # BYOK isolation: a caller that configured provider keys may only route to
    # those providers. Empty mapping keeps the operator/local path.
    #
    # LOCAL-FIRST: `routable_providers()` drops the demo provider as soon as a
    # real operator key exists, so synthetic traffic never mixes into real
    # usage numbers. With no keys at all it is the only option, which is what
    # makes a fresh clone work immediately.
    candidate_providers = routable_providers()
    if provider_api_keys:
        candidate_providers = [
            name for name in candidate_providers if name in provider_api_keys
        ]

    stats_snapshot = await _build_stats_snapshot(resolved_task_type, breaker, db_session)
    ranked = rank_models(
        resolved_task_type,
        candidate_providers,
        stats_snapshot,
        weights,
        prompt=prompt,
        ml_classifier=(
            get_router_classifier() if routing_method == "ml_hybrid" else None
        ),
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
        extra_walk_logs: list[AttemptLog] = []
        initial_model: Optional[str] = None
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
            # ---- Real attempt (shared lazy candidate-walking path) ----
            kwargs = _generation_kwargs(
                provider_name, resolved_task_type, temperature, max_tokens
            )
            byok_key = (
                provider_api_keys.get(provider_name) if provider_api_keys else None
            )
            caller_key = byok_key or _operator_env_key(provider_name)
            if byok_key:
                kwargs["provider_api_key"] = byok_key
            pinned_model = (
                (provider_models or {}).get(provider_name)
                if provider_models
                else None
            )

            try:
                response, extra_walk_logs, initial_model = (
                    await _execute_provider_attempt(
                        provider_name,
                        prompt,
                        kwargs,
                        caller_key,
                        pinned_model,
                    )
                )
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
                model=initial_model,
            )
        )
        # Per-candidate walk steps join the fallback chain so the
        # dashboard/Test view shows WHICH model failed and why.
        attempts.extend(extra_walk_logs)
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
        # BYOK isolation: a 401/403 caused by the CALLER's own provider key is
        # the caller's problem, not a provider outage — it must not OPEN the
        # global circuit breaker and lock the provider out for every other
        # caller. (Gate 1 above checks user-key auth BEFORE the global gate 2
        # circuit state only via the caller's key; the breaker stays global.)
        user_key_used = bool(
            provider_api_keys and provider_name in provider_api_keys
        )
        try:
            if response.success:
                await breaker.record_success(provider_name)
            elif response.outcome in CONFIG_ERROR_OUTCOMES:
                # 400 / 404 / 405 mean WE built the request wrong or named a
                # model that does not exist. The provider is up and answering;
                # opening the breaker would take a healthy provider out of
                # rotation for every caller because of one bad model string.
                #
                # A SUCCESS is recorded instead: the provider demonstrably
                # responded, which is the only thing the breaker is entitled to
                # conclude from this.
                await breaker.record_success(provider_name)
                logger.info(
                    "breaker_skipped_config_error",
                    request_id=request_id,
                    provider=provider_name,
                    outcome=response.outcome,
                    detail="config error (400/404) — the provider is reachable, "
                    "so this is not an outage",
                )
            elif user_key_used and response.error.startswith("auth_failed"):
                logger.info(
                    "breaker_skipped_user_key_auth_error",
                    request_id=request_id,
                    provider=provider_name,
                    task_type=resolved_task_type,
                    detail="401/403 from a caller-supplied BYOK key never "
                    "opens the global circuit breaker",
                )
            elif chaos_injected:
                # Chaos failures count too: repeated injections SHOULD be able
                # to trip the real breaker — realistic and demonstrable.
                await breaker.record_failure(provider_name)
            else:
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
                routing_method=routing_method,
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
        routing_method=routing_method,
    )
    await _persist_result(
        db_session, request_id, resolved_task_type, attempt_records, result
    )
    return result
