"""Per-caller provider key management (BYOK).

All endpoints require a valid RouteLLM ``X-API-Key`` and only ever touch rows
scoped to that calling key. Raw provider keys are accepted once on PUT, stored
encrypted, and never returned again. GET reports configuration status for every
registered provider so the dashboard can render a section per provider.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_api_key
from app.core.provider_key_crypto import ProviderKeyEncryptionError
from app.core.provider_key_manager import (
    SUPPORTED_PROVIDERS,
    delete_provider_key,
    is_supported_provider,
    list_provider_keys,
    upsert_provider_key,
)
from app.db.session import get_session
from app.models.api_key import ApiKeyDB
from app.models.provider_key import ProviderKeyDB

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
    return _status(provider_name, record)


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
    return DeleteProviderKeyResponse(status="deleted", provider_name=provider_name)
