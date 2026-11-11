"""Per-caller provider key management (BYOK) + model explorer.

All endpoints require a valid RouteLLM ``X-API-Key`` and only ever touch rows
scoped to that calling key. Raw provider keys are accepted once on PUT, stored
encrypted, and never returned again. GET reports configuration status for every
registered provider so the dashboard can render a section per provider.

Model explorer endpoints (all scoped per caller key, fail-soft, never
return a key):
- GET  /{provider}/models                    discovered list + statuses
- POST /{provider}/models/{model_id}/test    one 1-token request
- PUT  /{provider}/model                     pin a model (or null = Auto)

The Auto "Test connection" flow lives in ONE route only:
POST /api/v1/provider-test/{provider} (see provider_test_routes) — the same
candidate walk real routing uses, with the shared quota guard.
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import delete, select

from app.api.dependencies import require_api_key
from app.core import model_discovery as md
from app.core.provider_key_crypto import ProviderKeyEncryptionError
from app.core.provider_key_manager import (
    SUPPORTED_PROVIDERS,
    delete_provider_key,
    get_decrypted_provider_keys,
    is_supported_provider,
    list_provider_keys,
    upsert_provider_key,
)
from app.db.session import get_session
from app.models.api_key import utcnow
from app.models.api_key import ApiKeyDB
from app.models.provider_key import ProviderKeyDB
from app.models.provider_model_status import (
    ProviderModelPinDB,
    ProviderModelStatusDB,
)
from app.providers.registry import PROVIDER_REGISTRY

router = APIRouter(prefix="/provider-keys", tags=["Provider keys (BYOK)"])


class SetProviderKeyRequest(BaseModel):
    """One-time raw upstream provider key submission.

    Length is validated in the handler (not via Field constraints) so FastAPI
    validation errors never echo the submitted secret back in an ``input`` field.
    """

    api_key: str


class ProviderKeyStatusResponse(BaseModel):
    """Non-secret configuration status for one provider."""

    provider_name: str
    configured: bool
    key_hint: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class DeleteProviderKeyResponse(BaseModel):
    """Confirmation that a caller-scoped provider key was removed."""

    status: str
    provider_name: str


def _require_supported(provider_name: str) -> None:
    if not is_supported_provider(provider_name):
        raise HTTPException(status_code=404, detail="Unknown provider")


def _status(
    provider_name: str, record: Optional[ProviderKeyDB]
) -> ProviderKeyStatusResponse:
    if record is None:
        return ProviderKeyStatusResponse(provider_name=provider_name, configured=False)
    return ProviderKeyStatusResponse(
        provider_name=provider_name,
        configured=True,
        key_hint=record.key_hint,
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
    )


@router.get("", response_model=list[ProviderKeyStatusResponse])
async def get_provider_keys(
    api_key: ApiKeyDB = Depends(require_api_key),
    db_session: AsyncSession = Depends(get_session),
) -> list[ProviderKeyStatusResponse]:
    """List configuration status for every provider, scoped to the calling key."""
    rows = await list_provider_keys(db_session, api_key.id)
    by_provider = {row.provider_name: row for row in rows}
    return [_status(name, by_provider.get(name)) for name in SUPPORTED_PROVIDERS]


@router.put("/{provider_name}", response_model=ProviderKeyStatusResponse)
async def set_provider_key(
    provider_name: str,
    body: SetProviderKeyRequest,
    api_key: ApiKeyDB = Depends(require_api_key),
    db_session: AsyncSession = Depends(get_session),
) -> ProviderKeyStatusResponse:
    """Encrypt and store a provider key for the calling RouteLLM key."""
    _require_supported(provider_name)
    cleaned = body.api_key.strip()
    if len(cleaned) < 8 or len(cleaned) > 512:
        raise HTTPException(
            status_code=422,
            detail="Provider key must be between 8 and 512 characters.",
        )
    try:
        record = await upsert_provider_key(
            db_session, api_key.id, provider_name, cleaned
        )
    except ProviderKeyEncryptionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # Non-blocking discovery so the explorer is ready when the user looks.
    task = asyncio.create_task(_warm_discovery_after_save(provider_name, cleaned))
    task.add_done_callback(_swallow_task_exception)
    return _status(provider_name, record)


def _swallow_task_exception(task: "asyncio.Task") -> None:
    """Background discovery must never surface as an unhandled task error."""
    if not task.cancelled() and task.exception() is not None:
        from app.utils.logger import get_logger

        get_logger(__name__).debug(
            "post_save_discovery_task_suppressed", error=str(task.exception())
        )


@router.delete("/{provider_name}", response_model=DeleteProviderKeyResponse)
async def remove_provider_key(
    provider_name: str,
    api_key: ApiKeyDB = Depends(require_api_key),
    db_session: AsyncSession = Depends(get_session),
) -> DeleteProviderKeyResponse:
    """Delete the calling key's stored provider key for one provider."""
    _require_supported(provider_name)
    deleted = await delete_provider_key(db_session, api_key.id, provider_name)
    if not deleted:
        raise HTTPException(status_code=404, detail="Provider key not found")
    # The pin and test statuses belong to the (key, provider) pair — drop them.
    try:
        await db_session.execute(
            delete(ProviderModelPinDB).where(
                ProviderModelPinDB.api_key_id == api_key.id,
                ProviderModelPinDB.provider_name == provider_name,
            )
        )
        await db_session.execute(
            delete(ProviderModelStatusDB).where(
                ProviderModelStatusDB.api_key_id == api_key.id,
                ProviderModelStatusDB.provider_name == provider_name,
            )
        )
        await db_session.commit()
    except Exception:  # fail-soft: the key deletion already succeeded
        await db_session.rollback()
    return DeleteProviderKeyResponse(status="deleted", provider_name=provider_name)


# ==========================================================================
# Model explorer
# ==========================================================================


class ProviderModelInfo(BaseModel):
    """One discovered model as shown in the explorer (never a secret)."""

    id: str
    display_name: str
    released_or_version: Optional[str] = None
    sort_key: str
    tier: str
    tier_source: str = "estimated"
    context_window: Optional[int] = None
    max_output: Optional[int] = None
    capabilities: list[str] = []
    status: str = "untested"
    last_checked_at: Optional[str] = None
    last_error: Optional[str] = None


class ProviderModelsResponse(BaseModel):
    """Fail-soft model list: an empty list + reason instead of a 500."""

    provider_name: str
    models: list[ProviderModelInfo]
    hidden_non_chat_count: int = 0
    pinned_model: Optional[str] = None
    reason: Optional[str] = None


class ProviderModelTestResponse(BaseModel):
    provider_name: str
    model_id: str
    status: str
    latency_ms: float = 0.0
    error: Optional[str] = None


class SetPinnedModelRequest(BaseModel):
    """Pin a model (or null to return to Auto)."""

    model_id: Optional[str] = None


async def _require_decrypted_key(
    db_session: AsyncSession, api_key_id: int, provider_name: str
) -> str:
    """Decrypt the caller's stored key for one provider (404 when absent)."""
    _require_supported(provider_name)
    try:
        keys = await get_decrypted_provider_keys(db_session, api_key_id)
    except ProviderKeyEncryptionError as exc:
        raise HTTPException(
            status_code=503,
            detail="Provider key encryption is unavailable. Contact the operator.",
        ) from exc
    key = keys.get(provider_name)
    if not key:
        raise HTTPException(
            status_code=404, detail="Provider key not found for this caller"
        )
    return key


async def _warm_discovery_after_save(provider_name: str, raw_key: str) -> None:
    """Fire-and-forget discovery right after a key is saved (non-blocking).

    Short timeout, all failures swallowed: a provider outage must never turn
    a successful key save into an error.
    """
    try:
        models, reason = await asyncio.wait_for(
            md.discover_models(provider_name, raw_key, force=True),
            timeout=10.0,
        )
        from app.utils.logger import get_logger

        get_logger(__name__).info(
            "post_save_discovery_done",
            provider=provider_name,
            model_count=len(models),
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001 — truly best-effort
        from app.utils.logger import get_logger

        get_logger(__name__).warning(
            "post_save_discovery_failed",
            provider=provider_name,
            error=type(exc).__name__,
        )


@router.get("/{provider_name}/models", response_model=ProviderModelsResponse)
async def get_provider_models(
    provider_name: str,
    sort: str = "tier",
    tier: str = "all",
    search: str = "",
    refresh: bool = False,
    api_key: ApiKeyDB = Depends(require_api_key),
    db_session: AsyncSession = Depends(get_session),
) -> ProviderModelsResponse:
    """The caller's discovered model list for one provider (fail-soft)."""
    stored_key = await _require_decrypted_key(db_session, api_key.id, provider_name)
    models, reason = await md.discover_models(
        provider_name, stored_key, force=refresh
    )
    pinned = await md.get_provider_model_pin(db_session, api_key.id, provider_name)

    if not models:
        return ProviderModelsResponse(
            provider_name=provider_name, models=[], reason=reason
        )

    visible, hidden = md.sort_models_for_provider(
        models, provider_name, sort=sort, tier_filter=tier, search=search
    )

    # Merge in-memory (~1h TTL) and DB statuses; DB is the durable record.
    db_rows = await md.load_db_statuses(db_session, api_key.id, provider_name)
    info = []
    for model in visible:
        status = md.get_cached_status(stored_key, provider_name, model.id) or "untested"
        last_checked = None
        last_error = None
        row = db_rows.get(model.id)
        if row is not None:
            status = row.status
            last_checked = row.last_checked_at.isoformat()
            last_error = row.last_error
        version = ".".join(str(p) for p in model.version) if model.version else None
        info.append(
            ProviderModelInfo(
                id=model.id,
                display_name=model.display_name,
                released_or_version=version,
                sort_key=".".join(str(p) for p in (model.version or ()))
                + (f"+{model.created}" if model.created else ""),
                tier=md.estimated_tier(provider_name, model.id),
                tier_source="estimated",
                context_window=model.context_window,
                max_output=model.max_output,
                capabilities=model.capabilities,
                status=status,
                last_checked_at=last_checked,
                last_error=last_error,
            )
        )
    return ProviderModelsResponse(
        provider_name=provider_name,
        models=info,
        hidden_non_chat_count=hidden,
        pinned_model=pinned,
    )


def _classify_test_result(error: Optional[str]) -> str:
    """Honest status mapping — never reports model problems as auth errors."""
    if not error:
        return "works"
    if error.startswith("model_not_found"):
        return "denied"
    if error == "rate_limit_exceeded":
        return "rate_limited"
    if error == "auth_failed":
        return "auth_failed"
    return "error"


@router.post(
    "/{provider_name}/models/{model_id}/test",
    response_model=ProviderModelTestResponse,
)
async def test_provider_model(
    provider_name: str,
    model_id: str,
    api_key: ApiKeyDB = Depends(require_api_key),
    db_session: AsyncSession = Depends(get_session),
) -> ProviderModelTestResponse:
    """One 1-token request through the SAME code path as real routing.

    Each test spends the caller's provider quota, so the shared guard applies
    (10 tests/min per key, max 5 in flight — 429 + Retry-After beyond that).
    """
    if not md.is_valid_model_id(model_id):
        raise HTTPException(status_code=422, detail="Invalid model id")
    stored_key = await _require_decrypted_key(db_session, api_key.id, provider_name)

    from app.core.test_endpoint_guard import check_and_acquire, release

    check_and_acquire(api_key.id)
    try:
        walk = await md.walk_candidates(
            provider_name,
            "ping",
            {"max_tokens": 1, "provider_api_key": stored_key},
            stored_key,
            first_model=model_id,
            max_attempts=1,
            db_session=db_session,
            api_key_id=api_key.id,
        )
    finally:
        release(api_key.id)
    step = walk.steps[-1] if walk.steps else None
    status = _classify_test_result(walk.response.error)
    return ProviderModelTestResponse(
        provider_name=provider_name,
        model_id=model_id,
        status=status,
        latency_ms=step.latency_ms if step else 0.0,
        error=md.sanitize_error_message(walk.response.error or "")
        if walk.response.error
        else None,
    )


@router.put("/{provider_name}/model", response_model=ProviderModelsResponse)
async def set_pinned_model(
    provider_name: str,
    body: SetPinnedModelRequest,
    api_key: ApiKeyDB = Depends(require_api_key),
    db_session: AsyncSession = Depends(get_session),
) -> ProviderModelsResponse:
    """Pin one model for routing, or null to return to Auto.

    The id is validated against the strict charset AND the discovered list;
    injection attempts ("../x", URLs) are rejected with 422.
    """
    _require_supported(provider_name)
    if body.model_id is None:
        # Auto: remove the pin row entirely.
        await db_session.execute(
            delete(ProviderModelPinDB).where(
                ProviderModelPinDB.api_key_id == api_key.id,
                ProviderModelPinDB.provider_name == provider_name,
            )
        )
        await db_session.commit()
    else:
        if not md.is_valid_model_id(body.model_id):
            raise HTTPException(status_code=422, detail="Invalid model id")
        stored_key = await _require_decrypted_key(
            db_session, api_key.id, provider_name
        )
        models, reason = await md.discover_models(provider_name, stored_key)
        if not models:
            raise HTTPException(
                status_code=503,
                detail=f"Cannot validate model id: {reason or 'discovery failed'}",
            )
        if body.model_id not in {m.id for m in models}:
            raise HTTPException(
                status_code=422,
                detail="Model id is not in this key's discovered model list",
            )
        stmt = select(ProviderModelPinDB).where(
            ProviderModelPinDB.api_key_id == api_key.id,
            ProviderModelPinDB.provider_name == provider_name,
        )
        row = (await db_session.execute(stmt)).scalars().first()
        if row is None:
            row = ProviderModelPinDB(
                api_key_id=api_key.id,
                provider_name=provider_name,
                model_id=body.model_id,
            )
        else:
            row.model_id = body.model_id
            row.updated_at = utcnow()
        db_session.add(row)
        await db_session.commit()

    return await get_provider_models(
        provider_name,
        sort="tier",
        tier="all",
        search="",
        refresh=False,
        api_key=api_key,
        db_session=db_session,
    )
