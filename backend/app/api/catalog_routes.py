"""Public model catalog: curated specs merged with live provider telemetry."""

from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.executor import _ensure_shared_clients
from app.core.model_catalog import MODEL_CATALOG
from app.core.scorer import COST_PER_1K_TOKENS
from app.db.session import get_session
from app.models import ModelStatsDB
from app.providers.registry import PROVIDER_REGISTRY
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["Catalog"])


class CatalogEntry(BaseModel):
    """One browsable model entry with static specs and live aggregates."""

    provider_name: str
    display_name: str
    description: str
    context_window: int
    capabilities: list[str]
    pricing_tier: str
    cost_per_1k_tokens: float
    live_success_rate: float
    avg_latency_ms: float
    current_status: Literal["healthy", "degraded", "circuit_open"]


def _get_breaker():
    """Return the shared circuit breaker (injectable in tests)."""
    return _ensure_shared_clients()[0]


def _status_for(state: str, success_rate: float) -> str:
    """Same status thresholds as GET /api/v1/models."""
    if state == "OPEN":
        return "circuit_open"
    if state == "HALF_OPEN" or success_rate < 0.7:
        return "degraded"
    return "healthy"


@router.get("/catalog", response_model=list[CatalogEntry])
async def get_catalog(db_session: AsyncSession = Depends(get_session)):
    """Merge curated catalog metadata with live stats for registered providers."""
    try:
        breaker = _get_breaker()
        rows = (await db_session.execute(select(ModelStatsDB))).scalars().all()
        by_provider: dict[str, list[ModelStatsDB]] = {}
        for row in rows:
            by_provider.setdefault(row.provider_name, []).append(row)

        entries: list[CatalogEntry] = []
        for name in PROVIDER_REGISTRY:
            metadata = MODEL_CATALOG.get(name)
            if metadata is None:
                continue

            provider_rows = by_provider.get(name, [])
            total_requests = sum(row.total_requests for row in provider_rows)
            if total_requests > 0:
                success_rate = (
                    sum(row.successful_requests for row in provider_rows) / total_requests
                )
                avg_latency = (
                    sum(row.avg_latency_ms * row.total_requests for row in provider_rows)
                    / total_requests
                )
            else:
                # Match the dashboard's optimistic no-history default.
                success_rate = 0.75
                avg_latency = 0.0

            try:
                state = await breaker.get_state(name)
            except Exception as exc:
                logger.warning(
                    "catalog_circuit_state_unavailable", provider=name, error=str(exc)
                )
                state = "CLOSED"

            entries.append(
                CatalogEntry(
                    provider_name=name,
                    display_name=metadata["display_name"],
                    description=metadata["description"],
                    context_window=metadata["context_window"],
                    capabilities=list(metadata["capabilities"]),
                    pricing_tier=metadata["pricing_tier"],
                    cost_per_1k_tokens=COST_PER_1K_TOKENS.get(name, 0.0),
                    live_success_rate=round(success_rate, 3),
                    avg_latency_ms=round(avg_latency, 1),
                    current_status=_status_for(state, success_rate),
                )
            )
        return entries
    except Exception as exc:
        logger.error("catalog_endpoint_unexpected_error", error=str(exc))
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})
