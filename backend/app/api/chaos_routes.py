"""DEV/DEMO ONLY — Chaos admin endpoints (/api/v1/admin/chaos/*).

The router is always mounted, but every handler gates on
``chaos_mode_enabled()`` at request time and answers FastAPI's native 404
when Chaos Mode is off — the demo surface is unreachable in a deployment
where the flag resolves False (e.g. Hugging Face, or an explicit
``CHAOS_MODE_ENABLED=0``). Parsing is the same 404 you get for any unknown
path, so probes cannot even tell the feature exists.

The executor (core/executor.py) independently re-checks the same resolver
before honoring any chaos key, so even a stale Redis key from an earlier
chaotic session cannot affect a clean deployment.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import chaos_mode_enabled
from app.core.chaos import (
    DEFAULT_DURATION_SECONDS,
    ChaosController,
)
from app.core.executor import _ensure_shared_clients
from app.providers.registry import PROVIDER_REGISTRY
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _get_chaos() -> ChaosController:
    return _ensure_shared_clients()[2]


def _require_chaos_enabled() -> None:
    """404 (as if the route didn't exist) when Chaos Mode resolves off."""
    if not chaos_mode_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


chaos_router = APIRouter(
    tags=["chaos (DEV/DEMO ONLY)"],
    # Router-level gate so an OFF flag answers FastAPI's native 404 before
    # body validation (422) can even happen, i.e. exactly like an unknown path.
    dependencies=[Depends(_require_chaos_enabled)],
)


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
    _require_chaos_enabled()
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
    _require_chaos_enabled()
    await _get_chaos().clear_failure(body.provider_name)
    logger.info("chaos_cleared", provider=body.provider_name)
    return ChaosClearResponse(
        status="cleared", provider_name=body.provider_name
    )


@chaos_router.get("/admin/chaos/status", response_model=ChaosStatusResponse)
async def chaos_status():
    _require_chaos_enabled()
    active: Optional[list[str]] = await _get_chaos().get_all_active_chaos()
    return ChaosStatusResponse(active_chaos=active or [])
