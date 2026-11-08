"""DEV/DEMO ONLY — Chaos admin endpoints (/api/v1/admin/chaos/*).

LOCAL-FIRST: the flag is ON by default (this tool runs on your own machine
behind 127.0.0.1), and gating is evaluated **per request** by
:func:`require_chaos_enabled`. With ``CHAOS_MODE_ENABLED=false`` every path
here answers FastAPI's native 404 body — indistinguishable from a path that
does not exist — and the routes are also hidden from the OpenAPI /docs schema.
Evaluating the flag at request time (rather than at import time) keeps the
gate honest for anything that changes the setting after startup, and makes
the contract directly testable.

The executor (core/executor.py) independently re-checks
settings.CHAOS_MODE_ENABLED before honoring any chaos key, so even a stale
Redis key from an earlier chaotic session cannot affect a clean deployment.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.core.chaos import (
    DEFAULT_DURATION_SECONDS,
    ChaosController,
)
from app.core.executor import _ensure_shared_clients
from app.providers.registry import PROVIDER_REGISTRY
from app.utils.logger import get_logger

logger = get_logger(__name__)

async def require_chaos_enabled() -> None:
    """404 like an unknown path unless chaos mode is explicitly enabled.

    The body FastAPI produces for an unmatched route is ``{"detail": "Not
    Found"}``; raising the same shape here keeps the "disabled means
    unreachable" contract byte-for-byte.
    """
    if not settings.CHAOS_MODE_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")


chaos_router = APIRouter(
    tags=["chaos (DEV/DEMO ONLY)"],
    dependencies=[Depends(require_chaos_enabled)],
)


def _get_chaos() -> ChaosController:
    return _ensure_shared_clients()[2]


class ChaosInjectRequest(BaseModel):
    provider_name: str
    duration_seconds: int = Field(
        default=DEFAULT_DURATION_SECONDS, ge=1, le=3600
    )


class ChaosClearRequest(BaseModel):
    provider_name: str


class ChaosInjectResponse(BaseModel):
    status: str
    provider_name: str
    expires_in_seconds: int


class ChaosClearResponse(BaseModel):
    status: str
    provider_name: str


class ChaosStatusResponse(BaseModel):
    active_chaos: list[str]


@chaos_router.post("/admin/chaos/inject", response_model=ChaosInjectResponse)
async def inject_chaos(body: ChaosInjectRequest):
    if body.provider_name not in PROVIDER_REGISTRY:
        raise HTTPException(status_code=400, detail=f"unknown provider '{body.provider_name}'")
    ttl = await _get_chaos().inject_failure(
        body.provider_name, body.duration_seconds
    )
    logger.warning(
        "chaos_injection_requested",
        provider=body.provider_name,
        duration_seconds=ttl,
    )
    return ChaosInjectResponse(
        status="injected",
        provider_name=body.provider_name,
        expires_in_seconds=ttl,
    )


@chaos_router.post("/admin/chaos/clear", response_model=ChaosClearResponse)
async def clear_chaos(body: ChaosClearRequest):
    await _get_chaos().clear_failure(body.provider_name)
    logger.info("chaos_cleared", provider=body.provider_name)
    return ChaosClearResponse(
        status="cleared", provider_name=body.provider_name
    )


@chaos_router.get("/admin/chaos/status", response_model=ChaosStatusResponse)
async def chaos_status():
    active: Optional[list[str]] = await _get_chaos().get_all_active_chaos()
    return ChaosStatusResponse(active_chaos=active or [])
