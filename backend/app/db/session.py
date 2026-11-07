"""Async SQLAlchemy engine/session wiring.

Devices are created lazily on first use rather than at import time, so that
a missing DATABASE_URL (dev/test environments today) never breaks module
imports — kit raises a clear RuntimeError when actually needed instead.

NOTE: init_db() creates all tables straight from SQLModel.metadata. This is
fine for development, but MUST be replaced with Alembic migrations before
any real production use (versioned, reversible schema changes).
"""

from typing import AsyncGenerator, Optional

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel

import app.models  # noqa: F401  (registers all model tables on SQLModel.metadata)
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

_engine = None
_session_maker: Optional[async_sessionmaker] = None


class DatabaseUnavailableError(RuntimeError):
    """Raised when the configured database cannot be reached (or is not
    configured). API handlers convert this into a clean 503 instead of a
    500 stack trace — the gateway stays routable for STAT-less paths."""


# Provider-hosted Postgres URLs (Neon, Supabase, Heroku, Railway) commonly
# arrive with a bare protocol or psycopg-era query parameters the installed
# asyncpg dialect cannot forward (verified: they raise TypeError /
# NoSuchModuleError at engine connect). Both problems are fixed BEFORE the
# engine is built.
_DEFAULT_POSTGRES_DRIVER = "postgresql+asyncpg"
# Query params other stacks add that asyncpg rejects outright.
_ASYNCPG_INCOMPATIBLE_PARAMS = (
    "channel_binding",  # Supabase/psycopg
    "application_name",
    "statement_timeout",
    "options",  # psycopg server-settings syntax; asyncpg uses server_settings
    "endpoint",  # Supabase pooler suffix
)


def masked_database_url(raw: Optional[str]) -> str:
    """URL safe for logs/exceptions: keep scheme, host and db name, mask
    credentials.

    ``postgresql://user:secret@ep-1.neon.tech/routellm`` ->
    ``postgresql://user:****@ep-1.neon.tech/routellm``. Unparseable input
    degrades to ``<unparsable-database-url>`` so secrets never leak through
    an error path.
    """
    if not raw:
        return "(not configured)"
    try:
        url = make_url(raw)
    except Exception:
        return "<unparsable-database-url>"
    if url.username is None and url.password is None:
        return str(url)
    return url.render_as_string(hide_password=True)


def normalize_database_url(raw: str) -> str:
    """Accept Postgres URLs from ANY mainstream host and return one the
    installed asyncpg stack can dial:

    - ``postgres://`` and bare ``postgresql://`` → ``postgresql+asyncpg://``
      (asyncpg is our only installed Postgres driver).
    - ``sslmode=<value>`` → ``ssl=<value>`` (asyncpg accepts sslmode-style
      strings on its ``ssl`` argument, e.g. Neon's ``sslmode=require``).
    - psycopg-only query params (``channel_binding``,
      ``application_name``, ...) are dropped — asyncpg raises TypeError on
      them.
    - Non-postgres URLs (sqlite+aiosqlite in tests) pass through untouched.
    """
    if not raw:
        return raw
    try:
        url = make_url(raw)
    except Exception as exc:
        raise DatabaseUnavailableError(
            f"DATABASE_URL is not a valid connection URL "
            f"({masked_database_url(raw)}): {type(exc).__name__}"
        ) from exc

    if url.drivername.startswith("postgres"):
        if url.drivername in (
            "postgres",
            "postgresql",
            "postgresql+psycopg",
            "postgresql+psycopg2",
        ):
            url = url.set(drivername=_DEFAULT_POSTGRES_DRIVER)

        query = dict(url.query)
        channel_binding = query.pop("channel_binding", None)
        sslmode = query.pop("sslmode", None)
        if sslmode is not None and "ssl" not in query:
            query["ssl"] = str(sslmode)
        for param in _ASYNCPG_INCOMPATIBLE_PARAMS:
            query.pop(param, None)
        url = url.set(query=query)

    return url.render_as_string(hide_password=False)


def describe_database_driver() -> str:
    """Driver + host for the startup log — no credentials, ever."""
    if not settings.DATABASE_URL:
        return "not_configured"
    try:
        url = make_url(normalize_database_url(settings.DATABASE_URL))
        return f"{url.drivername}://{url.host}"
    except Exception:
        return "unknown"


def get_engine():
    global _engine
    if _engine is None:
        if not settings.DATABASE_URL:
            raise DatabaseUnavailableError(
                "DATABASE_URL is not configured. Set it in .env, e.g. "
                "postgresql+asyncpg://user:password@localhost:5432/routellm"
            )
        try:
            _engine = create_async_engine(
                normalize_database_url(settings.DATABASE_URL), echo=False
            )
        except Exception as e:  # malformed URL etc.
            raise DatabaseUnavailableError(f"database engine unavailable: {e}") from e
        logger.info("database_engine_started", driver=describe_database_driver())
    return _engine


def get_session_maker() -> async_sessionmaker:
    global _session_maker
    if _session_maker is None:
        _session_maker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_maker


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Async generator suitable for FastAPI Depends() injection.

    Failures to even OPEN a session raise DatabaseUnavailableError (mapped to
    503 by the app-level handler). Failures on first actual QUERY surface
    inside handlers as SQLAlchemy connect errors — the global handler also
    maps those, so callers never see a raw 500 for an infrastructure outage.
    """
    async with get_session_maker()() as session:
        yield session


async def init_db() -> None:
    """Create all tables, then add any columns created after the table did.

    NOTE: replace with Alembic migrations before production use.
    """
    async with get_engine().begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        # create_all() does NOT alter existing tables — add post-v1 columns
        # defensively (silently skipped if already present or if the table
        # was just created by create_all with the column included).
        for statement in ("ALTER TABLE task_logs ADD COLUMN routing_method VARCHAR",):
            try:
                await conn.execute(text(statement))
            except Exception:
                pass  # column already exists (or dialect-specific no-op)
