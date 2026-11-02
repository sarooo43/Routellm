"""Public model catalog: curated specs merged with live provider telemetry."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends
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
    """One browsable model entry with static specs and (optional) live aggregates.

    Live fields are None when the backing DB/Redis is unreachable — the static
    directory always renders (fail-soft); the UI shows "No live data" instead
    of an error page for a pure observability outage.
    """

    provider_name: str
    display_name: str
    description: str
    context_window: int
    capabilities: list[str]
    pricing_tier: str
    cost_per_1k_tokens: float
    live_success_rate: Optional[float] = None
    avg_latency_ms: Optional[float] = None
    current_status: Optional[Literal["healthy", "degraded", "circuit_open"]] = None


class CatalogResponse(BaseModel):
    """Static catalog + explicit flag telling the UI whether live telemetry
    was available (False → live fields are null, not zero)."""

    live_stats_available: bool
    models: list[CatalogEntry]


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


@router.get("/catalog", response_model=CatalogResponse)
async def get_catalog(db_session: AsyncSession = Depends(get_session)):
    """Merge curated catalog metadata with live stats for registered providers.

    FAIL-SOFT: the static directory (names, capabilities, context windows,
    pricing) is computed OUTSIDE any try/except and always renders. Only the
    live telemetry (stats/circuit state) is best-effort — any failure there
    yields null live fields and live_stats_available=false instead of an
    error, because observability must never take the directory down.
    """
    entries = _static_entries()

    # Live stats: aggregated per-provider request history. A DB outage nulls
    # the live fields — never the whole endpoint.
    live_stats_available = True
    by_provider: dict[str, list[ModelStatsDB]] = {}
    try:
        rows = (await db_session.execute(select(ModelStatsDB))).scalars().all()
        for row in rows:
            by_provider.setdefault(row.provider_name, []).append(row)
    except Exception as exc:
        logger.warning("catalog_live_stats_unavailable", error=str(exc))
        live_stats_available = False

    for entry in entries:
        if not live_stats_available:
            continue  # live fields stay null for every provider

        name = entry.provider_name
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
            state = await _get_breaker().get_state(name)
        except Exception as exc:
            logger.warning(
                "catalog_circuit_state_unavailable", provider=name, error=str(exc)
            )
            state = "CLOSED"

        entry.live_success_rate = round(success_rate, 3)
        entry.avg_latency_ms = round(avg_latency, 1)
        entry.current_status = _status_for(state, success_rate)

    return CatalogResponse(
        live_stats_available=live_stats_available, models=entries
    )


def _static_entries() -> list[CatalogEntry]:
    """Static catalog entries — pure dict lookups, cannot fail."""
    entries: list[CatalogEntry] = []
    for name in PROVIDER_REGISTRY:
        metadata = MODEL_CATALOG.get(name)
        if metadata is None:
            continue
        entries.append(
            CatalogEntry(
                provider_name=name,
                display_name=metadata["display_name"],
                description=metadata["description"],
                context_window=metadata["context_window"],
                capabilities=list(metadata["capabilities"]),
                pricing_tier=metadata["pricing_tier"],
                cost_per_1k_tokens=COST_PER_1K_TOKENS.get(name, 0.0),
            )
        )
    return entries
