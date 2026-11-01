"""Generation, validation, and usage accounting for lightweight demo API keys.

Security model: the database stores only a SHA-256 hash of the raw key plus a
short display prefix. The raw key is returned once at creation and cannot be
recovered from the stored row. This is deliberately NOT a full user/auth system.
"""

import hashlib
import secrets
from typing import Optional

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.api_key import ApiKeyDB, utcnow

KEY_PREFIX_LENGTH = 8


def hash_api_key(raw_key: str) -> str:
    """Return the hex SHA-256 digest of a raw API key."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Return (raw_key, key_hash, key_prefix).

    Raw keys are ``rk_`` followed by 32 URL-safe random characters. Only the
    hash and the first 8 characters are meant to be stored.
    """
    raw_key = "rk_" + secrets.token_urlsafe(24)[:32]
    return raw_key, hash_api_key(raw_key), raw_key[:KEY_PREFIX_LENGTH]


async def validate_api_key(
    raw_key: Optional[str], db_session: AsyncSession
) -> Optional[ApiKeyDB]:
    """Look up an active key by hash and touch last_used_at on success."""
    if not raw_key:
        return None

    stmt = select(ApiKeyDB).where(
        ApiKeyDB.key_hash == hash_api_key(raw_key),
        ApiKeyDB.is_active.is_(True),
    )
    record = (await db_session.execute(stmt)).scalars().first()
    if record is None:
        return None

    record.last_used_at = utcnow()
    await db_session.commit()
    await db_session.refresh(record)
    return record


async def record_api_key_usage(
    db_session: AsyncSession, api_key: ApiKeyDB, tokens_used: int
) -> None:
    """Atomically increment successful-request counters for the calling key."""
    stmt = (
        update(ApiKeyDB)
        .where(ApiKeyDB.id == api_key.id)
        .values(
            total_requests=ApiKeyDB.total_requests + 1,
            total_tokens_used=ApiKeyDB.total_tokens_used + max(0, tokens_used),
        )
    )
    await db_session.execute(stmt)
    await db_session.commit()
    await db_session.refresh(api_key)
