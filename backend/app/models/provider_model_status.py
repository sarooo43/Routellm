"""Per-caller model explorer state: tested statuses + pinned model.

Both tables are scoped to one RouteLLM caller key (``api_keys.id``) and one
upstream provider. Status rows record what a REAL 1-token test (or a routing
attempt) observed for one discovered model id — "denied" (e.g. Gemini's
"no longer available to new users"), "rate_limited" (429, transient, NOT
denied), or "works". The pinned row is the model the caller explicitly
chose; no row means Auto (candidate walking).
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Column, DateTime, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.models.api_key import utcnow


class ProviderModelStatusDB(SQLModel, table=True):
    """One (caller key, provider, model) test/status observation."""

    __tablename__ = "provider_model_statuses"
    __table_args__ = (
        UniqueConstraint(
            "api_key_id",
            "provider_name",
            "model_id",
            name="uq_provider_model_status_scope",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    api_key_id: int = Field(foreign_key="api_keys.id", index=True)
    provider_name: str = Field(index=True)
    # The bare model id (no "models/" prefix), strict charset
    # [a-zA-Z0-9._:/-] enforced at the API boundary.
    model_id: str = Field(index=True)
    # "untested" | "works" | "denied" | "rate_limited"
    status: str = Field(default="untested", index=True)
    last_checked_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    # Sanitized (never echoes the key) failure reason, truncated ~200 chars.
    last_error: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )


class ProviderModelPinDB(SQLModel, table=True):
    """One pinned model per (caller key, provider). No row = Auto."""

    __tablename__ = "provider_model_pins"
    __table_args__ = (
        UniqueConstraint(
            "api_key_id", "provider_name", name="uq_provider_model_pin_scope"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    api_key_id: int = Field(foreign_key="api_keys.id", index=True)
    provider_name: str = Field(index=True)
    model_id: str
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
