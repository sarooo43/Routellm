"""REST API layer — the Phase 7 public interface over the routing pipeline.

Endpoints (all under /api/v1):
- POST /route      run classify → rank → fallback execution, return the outcome
- GET  /models     per-provider health: breaker state + DB success rate + Redis quota
- GET  /logs       paginated/filtered TaskLog feed
- GET  /analytics  aggregate stats for the dashboard + cost-savings number
- GET  /health     liveness probe that also verifies DB/Redis (503 when down)

POST /route is rate-limited with slowapi. Every handler wraps its body in
try/except: unexpected failures become a generic 500 + structured log, never
a stack trace leaking to the caller.
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_api_key, require_provider_api_keys
from app.config import settings
from app.core.api_key_manager import record_api_key_usage
from app.core.circuit_breaker import CircuitBreaker
from app.core.executor import (
    ExecutionResult,
    _ensure_shared_clients,
    execute_with_fallback,
)
from app.core.quota_tracker import QuotaTracker
from app.core.chaos import ChaosController
from app.core.scorer import COST_PER_1K_TOKENS
from app.api.chaos_routes import chaos_router
from app.db.session import get_session
from app.models import ApiKeyDB, Feedback, ModelStatsDB, TaskLog
from app.providers.registry import PROVIDER_REGISTRY
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter()

# DEV/DEMO ONLY: chaos routes exist only when the flag is explicitly true.
# When off they are never registered — /api/v1/admin/chaos/* answers the
# native 404 exactly like any unknown path (and stays out of /docs).
if settings.CHAOS_MODE_ENABLED:
    router.include_router(chaos_router)

limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")


def _get_breaker() -> CircuitBreaker:
    return _ensure_shared_clients()[0]


def _get_quota() -> QuotaTracker:
    return _ensure_shared_clients()[1]


def _get_chaos() -> ChaosController:
    return _ensure_shared_clients()[2]


# --------------------------------------------------------------------------
# Analytics helpers (shared by /health and /api/v1/health).
# --------------------------------------------------------------------------


async def _health_components() -> dict[str, str]:
    components: dict[str, str] = {}

    try:
        breaker = _get_breaker()
        await breaker.redis.ping()
        components["redis"] = "ok"
    except Exception as e:
        logger.warning("health_redis_unreachable", error=str(e))
        components["redis"] = "down"

    if not settings.DATABASE_URL:
        components["database"] = "not_configured"
    else:
        try:
            from app.db.session import get_session_maker

            async with get_session_maker()() as session:
                await session.execute(text("SELECT 1"))
            components["database"] = "ok"
        except Exception as e:
            logger.warning("health_database_unreachable", error=str(e))
            components["database"] = "down"

    return components


async def health_payload() -> tuple[dict[str, Any], int]:
    """Returns (body, status_code) — 503 when any configured dependency is down."""
    components = await _health_components()
    any_down = "down" in components.values()
    status = "degraded" if any_down else "ok"
    payload = {"status": status, "service": "routellm-backend", "components": components}
    return payload, (503 if any_down else 200)


# --------------------------------------------------------------------------
# Request / response models (Pydantic v2).
# --------------------------------------------------------------------------


class RouteRequest(BaseModel):
    prompt: str
    task_type: Optional[str] = None
    max_fallbacks: int = 3
    # Benchmark-only naive baseline: route to a single fixed provider with
    # zero fallback, bypassing scoring/ranking entirely.
    force_provider: Optional[str] = None


class AttemptResponse(BaseModel):
    provider_name: str
    success: bool
    error: Optional[str] = None
    latency_ms: float
    chaos_injected: bool = False


class RouteResponse(BaseModel):
    success: bool
    result: str
    model_used: Optional[str]
    confidence_score: float
    fallback_chain_attempted: list[AttemptResponse]
    latency_ms: float
    tokens_used: int
    request_id: str
    error: Optional[str] = None
    # EXPERIMENTAL: "ml_hybrid" | "rule_based" — which capability source
    # ranked the providers for this request.
    routing_method: str = "rule_based"


class ProviderStatusResponse(BaseModel):
    name: str
    status: str
    success_rate_overall: float
    requests_today: int
    daily_quota_limit: int
    quota_percentage_used: float
    # DEV/DEMO ONLY — True while this provider has an active chaos injection.
    chaos_active: bool = False


class ModelsResponse(BaseModel):
    # Top-level switch mirrors settings.CHAOS_MODE_ENABLED so the frontend
    # only renders chaos controls when the backend actually allows them.
    chaos_mode_enabled: bool = False
    providers: list[ProviderStatusResponse]


class TaskLogResponse(BaseModel):
    request_id: str
    task_type: str
    selected_model: Optional[str] = None
    confidence_score: float
    status: str
    latency_ms: float
    tokens_used: int
    error_message: Optional[str] = None
    fallback_chain: list
    created_at: datetime
    # True when a thumbs rating already exists for this request — lets the
    # UI render the already-rated state on load without a failed API call.
    feedback_given: bool = False
    # EXPERIMENTAL: "ml_hybrid" | "rule_based" (default keeps old rows valid).
    routing_method: str = "rule_based"


class LogsResponse(BaseModel):
    total: int
    logs: list[TaskLogResponse]


class FeedbackRequest(BaseModel):
    request_id: str
    rating: str  # must be "positive" or "negative"


class FeedbackResponse(BaseModel):
    status: str
    request_id: str


class FeedbackCounts(BaseModel):
    positive: int
    negative: int
    positive_rate: float


class FeedbackSummaryResponse(BaseModel):
    total_rated: int
    positive_rate: float
    by_provider: dict[str, FeedbackCounts]
    by_task_type: dict[str, FeedbackCounts]


class AnalyticsResponse(BaseModel):
    total_requests: int
    overall_success_rate: float
    avg_latency_ms: float
    total_cost_estimate: float
    model_usage_breakdown: dict[str, int]
    estimated_cost_if_single_model: float
    requests_per_day: list[dict[str, Any]]


# --------------------------------------------------------------------------
# POST /api/v1/route — the main routing endpoint.
# --------------------------------------------------------------------------


@router.post("/route", response_model=RouteResponse)
@limiter.limit(settings.ROUTE_RATE_LIMIT)
async def route_prompt(
    request: Request,
    body: RouteRequest,
    db_session: AsyncSession = Depends(get_session),
    api_key: ApiKeyDB = Depends(require_api_key),
    provider_api_keys: dict[str, str] = Depends(require_provider_api_keys),
):
    try:
        start = time.perf_counter()
        if body.force_provider is not None and body.force_provider not in PROVIDER_REGISTRY:
            logger.warning(
                "route_rejected_unknown_force_provider",
                force_provider=body.force_provider,
            )
            return JSONResponse(
                status_code=400,
                content={"detail": f"unknown force_provider '{body.force_provider}'"},
            )
        result: ExecutionResult = await execute_with_fallback(
            body.prompt,
            db_session,
            task_type=body.task_type,
            max_fallbacks=body.max_fallbacks,
            force_provider=body.force_provider,
            provider_api_keys=provider_api_keys,
        )
        if result.success:
            await record_api_key_usage(db_session, api_key, result.tokens_used)
        logger.info(
            "route_endpoint_handled",
            request_id=result.request_id,
            success=result.success,
            model_used=result.model_used,
            wall_ms=round((time.perf_counter() - start) * 1000, 2),
        )
        return RouteResponse(
            success=result.success,
            result=result.result_text,
            model_used=result.model_used,
            confidence_score=result.confidence_score,
            fallback_chain_attempted=[
                AttemptResponse(**attempt.model_dump())
                for attempt in result.fallback_chain_attempted
            ],
            latency_ms=result.total_latency_ms,
            tokens_used=result.tokens_used,
            request_id=result.request_id,
            error=result.error,
            routing_method=result.routing_method,
        )
    except Exception as e:
        logger.error("route_endpoint_unexpected_error", error=str(e))
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# --------------------------------------------------------------------------
# GET /api/v1/models — live provider health snapshot.
# --------------------------------------------------------------------------


@router.get("/models", response_model=ModelsResponse)
async def get_models(db_session: AsyncSession = Depends(get_session)):
    try:
        breaker = _get_breaker()
        quota = _get_quota()
        items: list[ProviderStatusResponse] = []

        # DEV/DEMO ONLY: which providers currently have a forced failure.
        active_chaos: set[str] = set()
        if settings.CHAOS_MODE_ENABLED:
            try:
                active_chaos = set(await _get_chaos().get_all_active_chaos())
            except Exception as e:
                logger.warning("models_chaos_state_unavailable", error=str(e))

        for name in PROVIDER_REGISTRY:
            try:
                state = await breaker.get_state(name)
            except Exception as e:
                logger.warning(
                    "models_circuit_state_unavailable", provider=name, error=str(e)
                )
                state = "CLOSED"

            try:
                summary = await quota.get_usage_summary(name)
            except Exception as e:
                logger.warning(
                    "models_quota_summary_unavailable", provider=name, error=str(e)
                )
                summary = {
                    "requests_today": 0,
                    "tokens_today": 0,
                    "daily_limit": 0,
                    "percentage_used": 0.0,
                }

            try:
                stmt = select(ModelStatsDB).where(ModelStatsDB.provider_name == name)
                rows = (await db_session.execute(stmt)).scalars().all()
            except Exception as e:
                logger.warning(
                    "models_stats_query_unavailable", provider=name, error=str(e)
                )
                rows = []

            if rows:
                success_rate = round(
                    sum(row.success_rate for row in rows) / len(rows), 3
                )
            else:
                # No history yet — optimistic assumption, consistent with the scorer.
                success_rate = 0.75

            if state == "OPEN":
                status = "circuit_open"
            elif state == "HALF_OPEN" or success_rate < 0.7:
                status = "degraded"
            else:
                status = "healthy"

            items.append(
                ProviderStatusResponse(
                    name=name,
                    status=status,
                    success_rate_overall=success_rate,
                    requests_today=int(summary["requests_today"]),
                    daily_quota_limit=int(summary["daily_limit"]),
                    quota_percentage_used=float(summary["percentage_used"]),
                    chaos_active=name in active_chaos,
                )
            )
        return ModelsResponse(
            chaos_mode_enabled=settings.CHAOS_MODE_ENABLED, providers=items
        )
    except Exception as e:
        logger.error("models_endpoint_unexpected_error", error=str(e))
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# --------------------------------------------------------------------------
# GET /api/v1/logs — paginated, filtered TaskLog feed.
# --------------------------------------------------------------------------


@router.get("/logs", response_model=LogsResponse)
async def get_logs(
    db_session: AsyncSession = Depends(get_session),
    limit: int = 50,
    offset: int = 0,
    task_type: Optional[str] = None,
    status: Optional[str] = None,
):
    try:
        conditions = []
        if task_type:
            conditions.append(TaskLog.task_type == task_type)
        if status:
            conditions.append(TaskLog.status == status)

        count_stmt = select(func.count(TaskLog.id))
        if conditions:
            count_stmt = count_stmt.where(*conditions)
        total = (await db_session.execute(count_stmt)).scalar_one()

        stmt = (
            select(TaskLog)
            .order_by(TaskLog.created_at.desc())
            .offset(max(0, offset))
            .limit(max(1, min(limit, 500)))
        )
        if conditions:
            stmt = stmt.where(*conditions)
        rows = (await db_session.execute(stmt)).scalars().all()

        # Which of these (paginated) rows already carry a thumbs rating —
        # one lightweight query instead of one per row.
        rated_ids: set[str] = set()
        if rows:
            page_ids = [log.request_id for log in rows]
            rated_stmt = select(Feedback.request_id).where(
                Feedback.request_id.in_(page_ids)
            )
            rated_ids = set(
                (await db_session.execute(rated_stmt)).scalars().all()
            )

        log_responses = [
            TaskLogResponse.model_validate(log, from_attributes=True).model_copy(
                update={"feedback_given": log.request_id in rated_ids}
            )
            for log in rows
        ]

        return LogsResponse(
            total=int(total),
            logs=log_responses,
        )
    except Exception as e:
        logger.error("logs_endpoint_unexpected_error", error=str(e))
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# --------------------------------------------------------------------------
# GET /api/v1/analytics — dashboard aggregates + cost savings estimate.
# --------------------------------------------------------------------------


@router.get("/analytics", response_model=AnalyticsResponse)
async def get_analytics(db_session: AsyncSession = Depends(get_session)):
    try:
        total_requests = int(
            (await db_session.execute(select(func.count(TaskLog.id)))).scalar_one()
        )
        success_count = int(
            (
                await db_session.execute(
                    select(func.count(TaskLog.id)).where(TaskLog.status == "success")
                )
            ).scalar_one()
        )
        overall_success_rate = (
            round(success_count / total_requests, 4) if total_requests else 0.0
        )
        avg_latency = float(
            (await db_session.execute(select(func.avg(TaskLog.latency_ms)))).scalar_one()
            or 0.0
        )

        grouped = (
            await db_session.execute(
                select(
                    TaskLog.selected_model,
                    func.count(TaskLog.id),
                    func.sum(TaskLog.tokens_used),
                ).group_by(TaskLog.selected_model)
            )
        ).all()

        model_usage_breakdown: dict[str, int] = {name: 0 for name in PROVIDER_REGISTRY}
        total_cost = 0.0
        total_tokens_all = 0
        for model_name, count, tokens in grouped:
            task_tokens = int(tokens or 0)
            total_tokens_all += task_tokens
            if model_name:
                model_usage_breakdown[model_name] = (
                    model_usage_breakdown.get(model_name, 0) + int(count)
                )
                cost_per_1k = COST_PER_1K_TOKENS.get(model_name, 0.0)
                total_cost += (task_tokens / 1000.0) * cost_per_1k

        # Hypothetical: every request routed to the most expensive provider.
        most_expensive_per_1k = max(COST_PER_1K_TOKENS.values())
        cost_if_single_model = (total_tokens_all / 1000.0) * most_expensive_per_1k

        day_rows = (
            await db_session.execute(
                select(
                    func.date(TaskLog.created_at),
                    func.count(TaskLog.id),
                ).group_by(func.date(TaskLog.created_at))
            )
        ).all()
        counts_by_date = {str(day): int(count) for day, count in day_rows}

        today = datetime.now(timezone.utc).date()
        requests_per_day = []
        for i in range(13, -1, -1):
            key = (today - timedelta(days=i)).isoformat()
            requests_per_day.append({"date": key, "count": counts_by_date.get(key, 0)})

        return AnalyticsResponse(
            total_requests=total_requests,
            overall_success_rate=overall_success_rate,
            avg_latency_ms=round(avg_latency, 2),
            total_cost_estimate=round(total_cost, 6),
            model_usage_breakdown=model_usage_breakdown,
            estimated_cost_if_single_model=round(cost_if_single_model, 6),
            requests_per_day=requests_per_day,
        )
    except Exception as e:
        logger.error("analytics_endpoint_unexpected_error", error=str(e))
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# --------------------------------------------------------------------------
# POST /api/v1/feedback — human thumbs rating on a single routed response.
# --------------------------------------------------------------------------

VALID_RATINGS = {"positive", "negative"}


@router.post("/feedback", response_model=FeedbackResponse, status_code=201)
async def submit_feedback(
    body: FeedbackRequest,
    db_session: AsyncSession = Depends(get_session),
):
    try:
        if body.rating not in VALID_RATINGS:
            return JSONResponse(
                status_code=400,
                content={"detail": "rating must be 'positive' or 'negative'"},
            )

        log = (
            await db_session.execute(
                select(TaskLog).where(TaskLog.request_id == body.request_id)
            )
        ).scalars().first()
        if log is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"unknown request_id '{body.request_id}'"},
            )
        if log.status == "failed" or not log.selected_model:
            # No successful model → no response text exists to judge.
            return JSONResponse(
                status_code=400,
                content={
                    "detail": "cannot rate a failed request — no response was produced"
                },
            )

        existing = (
            await db_session.execute(
                select(Feedback).where(Feedback.request_id == body.request_id)
            )
        ).scalars().first()
        if existing is not None:
            # One rating per request — reject instead of silently overwriting.
            return JSONResponse(
                status_code=409,
                content={"detail": "feedback already exists for this request_id"},
            )

        db_session.add(
            Feedback(
                request_id=log.request_id,
                provider_name=log.selected_model,
                task_type=log.task_type,
                rating=body.rating,
            )
        )
        await db_session.commit()
        logger.info(
            "feedback_recorded",
            request_id=body.request_id,
            rating=body.rating,
            provider=log.selected_model,
            task_type=log.task_type,
        )
        return FeedbackResponse(status="recorded", request_id=body.request_id)
    except Exception as e:
        logger.error("feedback_endpoint_unexpected_error", error=str(e))
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# --------------------------------------------------------------------------
# GET /api/v1/feedback/summary — aggregate thumbs stats for the dashboard.
# --------------------------------------------------------------------------


def _counts_block(rows) -> dict[str, FeedbackCounts]:
    """Rows of (key, rating, count) → {key: {positive, negative, positive_rate}}."""
    out: dict[str, dict[str, int]] = {}
    for key, rating, count in rows:
        bucket = out.setdefault(str(key), {"positive": 0, "negative": 0})
        if rating == "positive":
            bucket["positive"] += int(count)
        elif rating == "negative":
            bucket["negative"] += int(count)
    return {
        key: FeedbackCounts(
            positive=v["positive"],
            negative=v["negative"],
            positive_rate=(
                round(v["positive"] / (v["positive"] + v["negative"]), 4)
                if (v["positive"] + v["negative"]) > 0
                else 0.0
            ),
        )
        for key, v in sorted(out.items())
    }


@router.get("/feedback/summary", response_model=FeedbackSummaryResponse)
async def get_feedback_summary(db_session: AsyncSession = Depends(get_session)):
    try:
        total_rated = int(
            (await db_session.execute(select(func.count(Feedback.id)))).scalar_one()
        )
        if total_rated == 0:
            return FeedbackSummaryResponse(
                total_rated=0,
                positive_rate=0.0,
                by_provider={},
                by_task_type={},
            )

        positive_total = int(
            (
                await db_session.execute(
                    select(func.count(Feedback.id)).where(
                        Feedback.rating == "positive"
                    )
                )
            ).scalar_one()
        )

        provider_rows = (
            await db_session.execute(
                select(
                    Feedback.provider_name, Feedback.rating, func.count(Feedback.id)
                ).group_by(Feedback.provider_name, Feedback.rating)
            )
        ).all()
        task_rows = (
            await db_session.execute(
                select(
                    Feedback.task_type, Feedback.rating, func.count(Feedback.id)
                ).group_by(Feedback.task_type, Feedback.rating)
            )
        ).all()

        return FeedbackSummaryResponse(
            total_rated=total_rated,
            positive_rate=round(positive_total / total_rated, 4),
            by_provider=_counts_block(provider_rows),
            by_task_type=_counts_block(task_rows),
        )
    except Exception as e:
        logger.error("feedback_summary_endpoint_unexpected_error", error=str(e))
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# --------------------------------------------------------------------------
# GET /api/v1/health — enhanced health probe (also aliased at root /health).
# --------------------------------------------------------------------------


@router.get("/health")
async def api_v1_health():
    payload, status_code = await health_payload()
    return JSONResponse(status_code=status_code, content=payload)
