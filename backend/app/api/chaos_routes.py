"""DEV/DEMO ONLY — Chaos admin endpoints (/api/v1/admin/chaos/*).

This router is mounted by app.api.routes **only** when
settings.CHAOS_MODE_ENABLED is true. When the flag is off these routes are
never registered at all, so unknown paths still answer FastAPI's native
404 — the demo surface is unreachable in a default production deployment
(also absent from the OpenAPI /docs schema).

The executor (core/executor.py) independently re-checks
settings.CHAOS_MODE_ENABLED before honoring any chaos key, so even a stale
Redis key from an earlier chaotic session cannot affect a clean deployment.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.chaos import (
    DEFAULT_DURATION_SECONDS,
    ChaosController,
)
from app.core.executor import _ensure_shared_clients
from app.providers.registry import PROVIDER_REGISTRY
from app.utils.logger import get_logger

logger = get_logger(__name__)

chaos_router = APIRouter(tags=["chaos (DEV/DEMO ONLY)"])


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
