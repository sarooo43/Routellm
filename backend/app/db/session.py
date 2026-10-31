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
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel

import app.models  # noqa: F401  (registers all model tables on SQLModel.metadata)
from app.config import settings

_engine = None
_session_maker: Optional[async_sessionmaker] = None


def get_engine():
    global _engine
    if _engine is None:
        if not settings.DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not configured. Set it in .env, e.g. "
                "postgresql+asyncpg://user:password@localhost:5432/routellm"
            )
        _engine = create_async_engine(settings.DATABASE_URL, echo=False)
    return _engine


def get_session_maker() -> async_sessionmaker:
    global _session_maker
    if _session_maker is None:
        _session_maker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_maker


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Async generator suitable for FastAPI Depends() injection."""
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
