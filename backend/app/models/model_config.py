from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, DateTime
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ModelConfigDB(SQLModel, table=True):
    __tablename__ = "models"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True)  # e.g. "groq", "gemini", "deepseek"
    provider: str  # display name, e.g. "Groq", "Google Gemini"
    cost_per_1k_tokens: float = 0.0
    status: str = "active"  # active, disabled
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True)),
    )
