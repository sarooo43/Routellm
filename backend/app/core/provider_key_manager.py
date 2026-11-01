"""CRUD + request-scoped decryption for per-caller provider keys (BYOK)."""

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import delete, select

from app.core.provider_key_crypto import (
    decrypt_provider_key,
    encrypt_provider_key,
    provider_key_hint,
)
from app.models.provider_key import ProviderKeyDB
from app.models.api_key import utcnow
from app.providers.registry import PROVIDER_REGISTRY
from app.utils.logger import get_logger

logger = get_logger(__name__)

SUPPORTED_PROVIDERS = tuple(PROVIDER_REGISTRY.keys())


def is_supported_provider(provider_name: str) -> bool:
    """Return True when the provider is registered with the routing engine."""
    return provider_name in PROVIDER_REGISTRY


async def list_provider_keys(
    db_session: AsyncSession, api_key_id: int
) -> list[ProviderKeyDB]:
    """List metadata rows for one caller key; never decrypts or returns secrets."""
    stmt = (
        select(ProviderKeyDB)
        .where(ProviderKeyDB.api_key_id == api_key_id)
        .order_by(ProviderKeyDB.provider_name)
    )
    return list((await db_session.execute(stmt)).scalars().all())


async def upsert_provider_key(
    db_session: AsyncSession,
    api_key_id: int,
    provider_name: str,
    raw_key: str,
) -> ProviderKeyDB:
    """Encrypt and store a provider key for one caller key (insert or update)."""
    encrypted = encrypt_provider_key(raw_key)
    hint = provider_key_hint(raw_key)

    stmt = select(ProviderKeyDB).where(
        ProviderKeyDB.api_key_id == api_key_id,
        ProviderKeyDB.provider_name == provider_name,
    )
    record = (await db_session.execute(stmt)).scalars().first()
    now = utcnow()
    if record is None:
        record = ProviderKeyDB(
            api_key_id=api_key_id,
            provider_name=provider_name,
            encrypted_key=encrypted,
            key_hint=hint,
            created_at=now,
            updated_at=now,
        )
        db_session.add(record)
    else:
        record.encrypted_key = encrypted
        record.key_hint = hint
        record.updated_at = now
        db_session.add(record)

    await db_session.commit()
    await db_session.refresh(record)
    logger.info(
        "provider_key_saved",
        api_key_id=api_key_id,
        provider=provider_name,
        key_hint=hint,
    )
    return record


async def delete_provider_key(
    db_session: AsyncSession, api_key_id: int, provider_name: str
) -> bool:
    """Delete one caller-scoped provider key. Returns True when a row existed."""
    stmt = delete(ProviderKeyDB).where(
        ProviderKeyDB.api_key_id == api_key_id,
        ProviderKeyDB.provider_name == provider_name,
    )
    result = await db_session.execute(stmt)
    await db_session.commit()
    deleted = (result.rowcount or 0) > 0
    if deleted:
        logger.info(
            "provider_key_deleted", api_key_id=api_key_id, provider=provider_name
        )
    return deleted


async def get_decrypted_provider_keys(
    db_session: AsyncSession, api_key_id: Optional[int]
) -> dict[str, str]:
    """Decrypt the caller's provider keys into an in-memory request mapping.

    Returns an empty mapping when the caller has no BYOK rows. Raises
    ProviderKeyEncryptionError when rows exist but cannot be decrypted, so the
    API can fail closed instead of silently falling back to operator keys.
    """
    if api_key_id is None:
        return {}
    rows = await list_provider_keys(db_session, api_key_id)
    return {row.provider_name: decrypt_provider_key(row.encrypted_key) for row in rows}
