"""Shared FastAPI dependencies."""

import hashlib
from typing import Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.api_key_manager import validate_api_key
from app.core.provider_key_crypto import ProviderKeyEncryptionError
from app.core.provider_key_manager import get_decrypted_provider_keys
from app.db.session import get_session
from app.models.api_key import ApiKeyDB

#: Hash of the implicit local-first identity. When REQUIRE_API_KEY is false a
#: request without X-API-Key is attributed to this single row, so usage
#: tracking / BYOK storage work with zero setup. The value is not a real key:
#: nothing about "local" is user-choosable and no endpoint accepts it as a
#: credential when auth is on.
_LOCAL_IDENTITY_PREFIX = "local-default"
_LOCAL_IDENTITY_HASH = hashlib.sha256(_LOCAL_IDENTITY_PREFIX.encode()).hexdigest()


async def _local_identity(db_session: AsyncSession) -> ApiKeyDB:
    """Get-or-create the single local-first identity row."""
    stmt = select(ApiKeyDB).where(ApiKeyDB.key_hash == _LOCAL_IDENTITY_HASH)
    record = (await db_session.execute(stmt)).scalars().first()
    if record is not None:
        return record
    record = ApiKeyDB(
        key_hash=_LOCAL_IDENTITY_HASH,
        key_prefix=_LOCAL_IDENTITY_PREFIX,
        label="Local (default identity)",
    )
    db_session.add(record)
    try:
        await db_session.commit()
    except IntegrityError:
        await db_session.rollback()  # concurrent request created it first
        record = (await db_session.execute(stmt)).scalars().first()
    except SQLAlchemyError as exc:
        await db_session.rollback()
        raise exc  # session dependency / global handler maps to 503
    if record is None:  # defensive: vanished between rollback and re-select
        raise SQLAlchemyError("local identity row disappeared")
    return record


async def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    db_session: AsyncSession = Depends(get_session),
) -> ApiKeyDB:
    """Attribute the request to an identity, enforcing auth only when enabled.

    ``REQUIRE_API_KEY=true`` (or a real X-API-Key): validate the key, 401 for
    missing, invalid, or revoked keys. ``REQUIRE_API_KEY=false`` (the
    local-first default): requests without a header run under the default
    local identity; requests *with* a valid header still resolve to that
    actual key so per-app usage tracking stays correct.
    """
    if x_api_key:
        api_key = await validate_api_key(x_api_key, db_session)
        if api_key is None:
            # An unrecognized key stays 401 even with auth disabled, so a
            # typo can never silently impersonate the local identity.
            raise HTTPException(
                status_code=401,
                detail="Invalid API key",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        return api_key
    if settings.REQUIRE_API_KEY:
        api_key = None
    else:
        api_key = await _local_identity(db_session)
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


async def require_provider_model_pins(
    api_key: ApiKeyDB = Depends(require_api_key),
    db_session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """The calling key's pinned model per provider (empty dict = all Auto).

    Routing precedence: pinned model, then the provider's .env model
    override, then Auto (candidate walking).
    """
    from app.core.model_discovery import load_provider_pins

    return await load_provider_pins(db_session, api_key.id)
