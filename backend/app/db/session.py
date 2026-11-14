"""Async SQLAlchemy engine/session wiring.

Design constraints this module has to satisfy at once:

* **Import-safe.** Nothing connects (or even validates a DSN) at import time,
  so ``import app.main`` works with no environment configured.
* **Boot-safe.** :func:`init_db` never raises: a Space that starts before Neon
  is reachable must still serve ``/health`` and the static catalog. The
  failure is logged (masked) and retried on the next request.
* **Self-healing.** Table creation is retried until it succeeds, then marked
  done. A later DB outage does not tear the schema down.
* **Read-only friendly.** No files are written; asyncpg needs no local state.

NOTE: ``init_db()`` creates tables straight from ``SQLModel.metadata``. That is
fine for this project's scope; real production use wants Alembic migrations.
"""

import asyncio
from typing import AsyncGenerator, Optional

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel

import app.models  # noqa: F401  (registers all model tables on SQLModel.metadata)
from app.config import settings
from app.local import resolve_database_url
from app.db.url import (
    InvalidDatabaseUrl,
    describe_redis_url,  # noqa: F401  (re-exported for convenience)
    mask_database_url,
    normalize_database_url,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

_engine: Optional[AsyncEngine] = None
_session_maker: Optional[async_sessionmaker] = None
#: Set once ``create_all`` has succeeded; guards against re-running it on
#: every request (it is idempotent but not free).
_schema_ready = False
_engine_lock = asyncio.Lock()

#: Columns added after the first release. ``create_all`` never ALTERs an
#: existing table, so these are applied defensively (already-present errors
#: are swallowed on purpose).
_POST_V1_COLUMNS = ("ALTER TABLE task_logs ADD COLUMN routing_method VARCHAR",)


class DatabaseUnavailableError(RuntimeError):
    """Raised when the configured database cannot be reached (or is not
    configured). API handlers convert this into a clean 503 instead of a
    500 stack trace — the gateway stays routable for STAT-less paths.
    """


def effective_database_url() -> Optional[str]:
    """The DSN actually in use: explicit ``DATABASE_URL`` or the SQLite default.

    Local-first: with nothing configured this resolves to
    ``sqlite+aiosqlite:///<data dir>/routellm.db``, so a fresh clone has a
    working database with no setup. Returns ``None`` only if the local
    directory could not be created.
    """
    try:
        return resolve_database_url(settings.DATABASE_URL)
    except OSError as exc:  # data dir unusable (read-only host, etc.)
        logger.warning("local_database_unavailable", error=str(exc)[:200])
        return None


def database_url_normalized() -> str:
    """Return the normalized async DSN, raising DatabaseUnavailableError.

    The error message contains **only** the masked DSN, so a misconfigured
    Space logs ``postgresql+asyncpg://user:****@host/db`` and never the
    password.
    """
    raw = effective_database_url()
    if not raw:
        raise DatabaseUnavailableError(
            "No database is configured and the local data directory is not "
            "writable. Set ROUTELLM_DATA_DIR to a writable path, or set "
            "DATABASE_URL, e.g. postgresql+asyncpg://user:password@localhost:5432/routellm"
        )
    try:
        return normalize_database_url(raw).url
    except InvalidDatabaseUrl as exc:
        raise DatabaseUnavailableError(
            f"DATABASE_URL is invalid: {exc} (value: {mask_database_url(raw)})"
        ) from exc


def _is_sqlite(url: str) -> bool:
    """True for an aiosqlite DSN (no pool sizing, no pre-ping)."""
    return url.lower().startswith("sqlite")


def sqlite_connect_args() -> dict[str, float]:
    """``connect_args`` for a file-backed SQLite database.

    ``timeout`` is SQLite's `busy_timeout`: a concurrent write waits instead of
    raising ``database is locked`` immediately. The dashboard polling while a
    routing log commits is exactly that case.
    """
    return {"timeout": 30.0}


def get_engine() -> AsyncEngine:
    """Lazily build the async engine from the normalized DSN.

    Pooling notes: managed Postgres (Neon) sits behind a pooler, so a small
    client pool is both sufficient and safer than the default 5+10.
    ``pool_pre_ping`` recycles connections that the pooler closed while the
    Space was asleep, which is the usual cause of "connection was closed"
    errors after a cold start.
    """
    global _engine
    if _engine is None:
        url = database_url_normalized()
        try:
            if _is_sqlite(url):
                # SQLite has no server: pre-ping/pool-recycle are meaningless,
                # and a file-backed DB keeps a single writer. Pooling settings
                # are left at SQLAlchemy's async defaults for aiosqlite.
                _engine = create_async_engine(
                    url, echo=False, connect_args=sqlite_connect_args()
                )
            else:
                _engine = create_async_engine(
                    url,
                    echo=False,
                    pool_pre_ping=True,
                    pool_recycle=1800,
                )
        except Exception as e:  # malformed URL that passed normalization
            raise DatabaseUnavailableError(
                f"database engine unavailable: {e} "
                f"(value: {mask_database_url(effective_database_url() or '')})"
            ) from e
    return _engine


def get_session_maker() -> async_sessionmaker:
    global _session_maker
    if _session_maker is None:
        _session_maker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_maker


async def reset_engine() -> None:
    """Dispose and forget the cached engine (tests, and after a hard outage)."""
    global _engine, _session_maker, _schema_ready
    if _engine is not None:
        try:
            await _engine.dispose()
        except Exception:  # pragma: no cover - disposal is best effort
            pass
    _engine = None
    _session_maker = None
    _schema_ready = False


def schema_ready() -> bool:
    """True once ``create_all`` succeeded on this process."""
    return _schema_ready


async def ensure_schema() -> bool:
    """Create missing tables. Returns True on success, never raises.

    Called from the startup hook and retried from the session dependency while
    ``schema_ready()`` is False, so a first boot against a brand-new empty
    database (or a temporarily unreachable one) converges without manual
    steps.
    """
    global _schema_ready
    if _schema_ready:
        return True
    if not effective_database_url():
        return False
    async with _engine_lock:
        if _schema_ready:  # another coroutine won the race
            return True
        try:
            engine = get_engine()
            async with engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
                for statement in _POST_V1_COLUMNS:
                    try:
                        await conn.execute(text(statement))
                    except SQLAlchemyError:
                        pass  # column already exists
            _schema_ready = True
            logger.info(
                "database_schema_ready",
                database=mask_database_url(effective_database_url() or ""),
            )
            return True
        except DatabaseUnavailableError as e:
            logger.warning("database_schema_skipped", error=str(e))
        except (SQLAlchemyError, OSError) as e:
            logger.warning(
                "database_schema_failed_will_retry",
                database=mask_database_url(effective_database_url() or ""),
                error=str(e).strip()[:400],
            )
        except Exception as e:  # defensive: never break startup
            logger.warning("database_schema_unexpected_error", error=str(e)[:400])
        return False


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Async generator suitable for FastAPI Depends() injection.

    Failures to even OPEN a session raise DatabaseUnavailableError (mapped to
    503 by the app-level handler). Failures on first actual QUERY surface
    inside handlers as SQLAlchemy connect errors — the global handler also
    maps those, so callers never see a raw 500 for an infrastructure outage.
    One gap that handler needed help with: asyncpg leaks a RAW builtin
    ConnectionRefusedError for a refused TCP connect (it is an OSError, not a
    SQLAlchemyError) — main.py maps it to the same 503.

    While the schema has not been created yet (first boot of a brand-new DB),
    this retries ``create_all`` before handing out a session. That is what
    makes "empty database, zero manual steps" work even when the DB came up
    after the Space did.
    """
    if not _schema_ready:
        await ensure_schema()
    async with get_session_maker()() as session:
        yield session


async def get_optional_session() -> AsyncGenerator[Optional[AsyncSession], None]:
    """Like :func:`get_session` but yields ``None`` instead of raising 503.

    Used by read-only endpoints that must keep serving static data during a
    database outage (/api/v1/models, /api/v1/catalog). Callers treat ``None``
    exactly like a failed query: live fields go null, static content renders.
    """
    try:
        if not _schema_ready:
            await ensure_schema()
        async with get_session_maker()() as session:
            yield session
    except Exception as e:
        logger.warning(
            "optional_session_unavailable",
            database=mask_database_url(effective_database_url() or "") or None,
            error=str(e).strip()[:300],
        )
        yield None


async def init_db() -> None:
    """Backwards-compatible alias for :func:`ensure_schema` that never raises.

    Existing callers (lifespan) used to let a failure propagate and kill
    startup; a sleeping/empty managed database is a normal condition, not a
    fatal one.
    """
    await ensure_schema()


async def ping_database(timeout: float = 2.0) -> str:
    """Return ``"ok"``/``"down"``/``"not_configured"`` for the health probe."""
    if not effective_database_url():
        return "not_configured"
    try:
        async def _probe() -> None:
            async with get_session_maker()() as session:
                await session.execute(text("SELECT 1"))

        await asyncio.wait_for(_probe(), timeout=timeout)
        return "ok"
    except asyncio.TimeoutError:
        logger.warning("health_database_timeout", timeout_s=timeout)
        return "down"
    except Exception as e:
        logger.warning(
            "health_database_unreachable",
            database=mask_database_url(effective_database_url() or ""),
            error=str(e).strip()[:300],
        )
        return "down"
