"""Shared FastAPI dependencies."""

from typing import Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.api_key_manager import get_local_identity, validate_api_key
from app.core.provider_key_crypto import ProviderKeyEncryptionError
from app.core.provider_key_manager import get_decrypted_provider_keys
from app.db.session import get_session
from app.models.api_key import ApiKeyDB
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _describe_presented_key(raw_key: str) -> str:
    """Log-safe description of a presented credential (never the value)."""
    return f"prefix:{raw_key[:4]}…, length:{len(raw_key)}"


async def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    db_session: AsyncSession = Depends(get_session),
) -> ApiKeyDB:
    """Resolve the caller's identity.

    Two modes, both explicit:

    * ``REQUIRE_API_KEY=true`` — today's behaviour: ``X-API-Key`` must hash to an
      active row. Missing, unknown and revoked keys are all 401 with
      ``WWW-Authenticate: ApiKey``.
    * ``REQUIRE_API_KEY=false`` (local-first default) — no header is needed and
      the request is attributed to the stable local identity row (see
      ``api_key_manager.get_local_identity``), so usage counters and per-caller
      BYOK storage keep working. A key that IS presented is still validated, so
      adding one later changes nothing about existing clients.

    ``Authorization: Bearer <key>`` is accepted as well, because that is how the
    official OpenAI SDK sends credentials to ``/v1/chat/completions``.
    """
    presented = (x_api_key or "").strip() or _bearer_token(authorization)

    if presented:
        api_key = await validate_api_key(presented, db_session)
        if api_key is None and settings.REQUIRE_API_KEY:
            logger.warning("api_key_rejected", presented=_describe_presented_key(presented))
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing API key",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        if api_key is None:
            # Local mode: a stale key in a script must not break the run, but
            # the anonymous fallback is loud in the logs.
            logger.info("api_key_ignored_local_mode", presented=_describe_presented_key(presented))
        if api_key is not None:
            return api_key

    if settings.REQUIRE_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    return await get_local_identity(db_session)


def _bearer_token(authorization: Optional[str]) -> str:
    """Extract the token from a ``Bearer`` header, or ``""``."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


async def require_provider_api_keys(
    api_key: ApiKeyDB = Depends(require_api_key),
    db_session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Decrypt the calling identity's BYOK provider keys for one request.

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
