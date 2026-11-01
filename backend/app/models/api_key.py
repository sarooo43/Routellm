"""Standalone API-key records for the lightweight, no-login demo auth layer."""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, DateTime
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class ApiKeyDB(SQLModel, table=True):
    """A hashed API key. The raw secret is never persisted in the database."""

    __tablename__ = "api_keys"

    id: Optional[int] = Field(default=None, primary_key=True)
    key_hash: str = Field(unique=True, index=True)
    # First 8 characters of the raw key ("rk_" + 5 random chars) for display.
    key_prefix: str = Field(index=True)
    label: str = "Unnamed Key"
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    last_used_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    total_requests: int = 0
    total_tokens_used: int = 0
    is_active: bool = True
