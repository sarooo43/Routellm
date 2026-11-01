"""Shared FastAPI dependencies."""

from typing import Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_key_manager import validate_api_key
from app.core.provider_key_crypto import ProviderKeyEncryptionError
from app.core.provider_key_manager import get_decrypted_provider_keys
from app.db.session import get_session
from app.models.api_key import ApiKeyDB


async def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    db_session: AsyncSession = Depends(get_session),
) -> ApiKeyDB:
    """Validate X-API-Key, returning 401 for missing, invalid, or revoked keys."""
    api_key = await validate_api_key(x_api_key, db_session)
    if api_key is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return api_key


async def require_provider_api_keys(
    api_key: ApiKeyDB = Depends(require_api_key),
    db_session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Decrypt the calling key's BYOK provider keys for one request.

    Returns an empty mapping when the caller has not stored any provider keys
    (legacy operator/demo mode). Fails closed with 503 when encrypted rows exist
    but the server encryption key is missing or wrong, so routing never silently
    falls back to operator credentials for a BYOK caller.
    """
    try:
        return await get_decrypted_provider_keys(db_session, api_key.id)
    except ProviderKeyEncryptionError as exc:
        raise HTTPException(
            status_code=503,
            detail="Provider key encryption is unavailable. Contact the operator.",
        ) from exc
