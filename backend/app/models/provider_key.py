"""Per-caller provider API keys (BYOK), encrypted at rest.

Each row belongs to one RouteLLM caller key (``api_keys.id``) and one upstream
provider. Only the Fernet ciphertext and a short display hint are persisted;
the raw provider key is never stored.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Column, DateTime, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.models.api_key import utcnow


class ProviderKeyDB(SQLModel, table=True):
    """An encrypted upstream provider key scoped to a RouteLLM API key."""

    __tablename__ = "provider_keys"
    __table_args__ = (
        UniqueConstraint("api_key_id", "provider_name", name="uq_provider_key_scope"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    api_key_id: int = Field(foreign_key="api_keys.id", index=True)
    provider_name: str = Field(index=True)
    encrypted_key: str = Field(sa_column=Column(Text, nullable=False))
    key_hint: str = "****"
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
