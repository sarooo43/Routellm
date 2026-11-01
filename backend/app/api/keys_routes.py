"""Self-service API-key endpoints for the lightweight no-login demo.

Generation is public. A key can only read/revoke itself via X-API-Key; there is
no list-all endpoint and no user/account system.
"""

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_api_key
from app.core.api_key_manager import generate_api_key
from app.db.session import get_session
from app.models.api_key import ApiKeyDB
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/keys", tags=["API keys"])


class GenerateKeyRequest(BaseModel):
    """Optional human-friendly label for a newly generated key."""

    label: Optional[str] = Field(default="Unnamed Key", max_length=120)


class GenerateKeyResponse(BaseModel):
    """One-time secret disclosure plus display metadata."""

    api_key: str
    key_prefix: str
    label: str
    warning: str


class KeyUsageResponse(BaseModel):
    """Usage stats for the calling key only."""

    key_prefix: str
    label: str
    created_at: str
    total_requests: int
    total_tokens_used: int
    last_used_at: Optional[str] = None


class RevokeKeyResponse(BaseModel):
    """Confirmation that the calling key was deactivated."""

    status: str
    key_prefix: str


@router.post("/generate", response_model=GenerateKeyResponse, status_code=201)
async def generate_key(
    body: GenerateKeyRequest | None = None,
    db_session: AsyncSession = Depends(get_session),
) -> GenerateKeyResponse:
    """Create a key, store only its hash, and return the raw secret once."""
    label = (body.label if body and body.label else None) or "Unnamed Key"
    raw_key, key_hash, key_prefix = generate_api_key()
    record = ApiKeyDB(key_hash=key_hash, key_prefix=key_prefix, label=label)
    db_session.add(record)
    await db_session.commit()
    logger.info("api_key_generated", key_prefix=key_prefix)
    return GenerateKeyResponse(
        api_key=raw_key,
        key_prefix=key_prefix,
        label=label,
        warning="Save this key now — it will not be shown again.",
    )


@router.get("/me", response_model=KeyUsageResponse)
async def get_my_key(
    api_key: ApiKeyDB = Depends(require_api_key),
) -> KeyUsageResponse:
    """Return usage stats for the key presented in X-API-Key."""
    return KeyUsageResponse(
        key_prefix=api_key.key_prefix,
        label=api_key.label,
        created_at=api_key.created_at.isoformat(),
        total_requests=api_key.total_requests,
        total_tokens_used=api_key.total_tokens_used,
        last_used_at=api_key.last_used_at.isoformat() if api_key.last_used_at else None,
    )


@router.delete("/me", response_model=RevokeKeyResponse)
async def revoke_my_key(
    api_key: ApiKeyDB = Depends(require_api_key),
    db_session: AsyncSession = Depends(get_session),
) -> RevokeKeyResponse:
    """Deactivate the calling key. The row is retained for usage history."""
    api_key.is_active = False
    db_session.add(api_key)
    await db_session.commit()
    logger.info("api_key_revoked", key_prefix=api_key.key_prefix)
    return RevokeKeyResponse(status="revoked", key_prefix=api_key.key_prefix)
