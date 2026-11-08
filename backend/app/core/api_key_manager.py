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
from app.utils.logger import get_logger

logger = get_logger(__name__)

KEY_PREFIX_LENGTH = 8

#: Label of the implicit identity that owns usage when ``REQUIRE_API_KEY`` is
#: false (the local-first default). It is a real row, so per-caller BYOK
#: storage and usage counters keep working — and survives restarts because it
#: is written to the same SQLite database as everything else.
LOCAL_IDENTITY_LABEL = "Local (no API key required)"
#: Prefix shown in the UI for that identity.
LOCAL_IDENTITY_PREFIX = "local"
#: Stable hash for the local row. Not a secret: it can never be presented as a
#: key because ``validate_api_key`` hashes the presented secret first, and no
#: raw secret hashes to this value (it is not the SHA-256 of anything the app
#: issues). Keeping it deterministic is what makes the row idempotent.
_LOCAL_IDENTITY_HASH = "sha256:local-identity"


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


async def get_local_identity(db_session: AsyncSession) -> ApiKeyDB:
    """Return (creating on first use) the implicit local identity row.

    LOCAL-FIRST: with ``REQUIRE_API_KEY=false`` the app is usable straight after
    ``python run.py`` with no key to copy around. Usage still has to belong to
    *something* — BYOK provider keys are stored per API-key row — so an
    anonymous run is attributed to this single stable row instead of being
    unauthenticated.

    The row is created inside the caller's transaction and looked up by a fixed
    hash, so it is created exactly once per database and survives restarts.
    """
    stmt = select(ApiKeyDB).where(ApiKeyDB.key_hash == _LOCAL_IDENTITY_HASH)
    record = (await db_session.execute(stmt)).scalars().first()
    if record is not None:
        if not record.is_active:
            # Revoking "the local key" would silently lock the user out of
            # their own machine, so reactivate instead of failing closed.
            record.is_active = True
            db_session.add(record)
            await db_session.commit()
            await db_session.refresh(record)
        return record

    record = ApiKeyDB(
        key_hash=_LOCAL_IDENTITY_HASH,
        key_prefix=LOCAL_IDENTITY_PREFIX,
        label=LOCAL_IDENTITY_LABEL,
    )
    db_session.add(record)
    await db_session.commit()
    await db_session.refresh(record)
    logger.info("local_identity_created", label=LOCAL_IDENTITY_LABEL)
    return record
