"""Redis/Dragonfly client construction shared by the whole app.

Managed Redis (Upstash) is TLS-only and remote, which changes two defaults that
matter for a small deployment:

* the DSN must be ``rediss://`` (see docs/deploy/HUGGINGFACE_AND_MANAGED.md troubleshooting),
* every command needs a **short socket timeout**: a sleeping or unreachable
  Redis must never hold a request open, and ``/health`` must answer within its
  probe budget.

Without ``REDIS_URL`` the app falls back to in-process fakeredis so a laptop
needs zero setup. That fallback is dev/demo only — it is not shared between
instances and does not survive a restart.
"""

import asyncio
from typing import Optional

import redis.asyncio as redis_async

from app.config import normalize_redis_url, redis_uses_tls, settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Socket budget for ordinary commands. Small on purpose: circuit-breaker and
#: quota lookups happen inside a request.
REDIS_SOCKET_TIMEOUT_S = 2.0
#: Budget for the /health probe (matches HEALTH_PROBE_TIMEOUT_S semantics).
REDIS_HEALTH_TIMEOUT_S = 2.0


def create_redis_client(url: Optional[str] = None):
    """Build a Redis client (TLS when the DSN is ``rediss://``).

    Raises ``ValueError`` for a non-Redis scheme — callers that must not fail
    (health, startup log) catch it and report the component as unavailable.
    """
    raw = url if url is not None else settings.REDIS_URL
    normalized = normalize_redis_url(raw)
    if normalized is None:
        # Local-first: no Redis is the normal state, not a degraded one. Only a
        # URL that was *set but unusable* is a warning worth waking someone for.
        if (raw or "").strip():
            logger.warning(
                "redis_unavailable_fallback",
                detail="REDIS_URL is not a redis:// or rediss:// DSN; using the "
                "in-memory store instead. State does not survive restarts and "
                "is not shared across instances.",
            )
        else:
            logger.debug(
                "redis_in_process_store",
                detail="REDIS_URL unset — using the in-memory store.",
            )
        from fakeredis import FakeAsyncRedis

        return FakeAsyncRedis(decode_responses=True)

    client = redis_async.from_url(
        normalized,
        decode_responses=True,
        socket_timeout=REDIS_SOCKET_TIMEOUT_S,
        socket_connect_timeout=REDIS_SOCKET_TIMEOUT_S,
        health_check_interval=30,
        # Keep a small pool: Spaces are single-process and Upstash bills
        # per connection.
        max_connections=10,
    )
    logger.info(
        "redis_client_created",
        scheme="rediss" if redis_uses_tls(normalized) else "redis",
        tls=redis_uses_tls(normalized),
    )
    return client


def describe_redis() -> str:
    """Log-safe Redis description (scheme + host, never the password)."""
    from app.db.url import describe_redis_url

    return describe_redis_url(settings.REDIS_URL)


async def ping_redis(client, timeout: float = REDIS_HEALTH_TIMEOUT_S) -> str:
    """Return ``"ok"``/``"down"`` for the health probe. Never raises."""
    try:
        result = await asyncio.wait_for(client.ping(), timeout=timeout)
        return "ok" if result else "down"
    except asyncio.TimeoutError:
        logger.warning("health_redis_timeout", timeout_s=timeout)
        return "down"
    except Exception as e:
        logger.warning("health_redis_unreachable", error=str(e).strip()[:300])
        return "down"
