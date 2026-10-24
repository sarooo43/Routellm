from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, DateTime, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ModelStatsDB(SQLModel, table=True):
    __tablename__ = "model_stats"
    __table_args__ = (
        UniqueConstraint("provider_name", "task_type"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_name: str = Field(index=True)
    task_type: str = Field(index=True)
    total_requests: int = 0
    successful_requests: int = 0
    avg_latency_ms: float = 0.0
    # Denormalized for fast reads; recomputed on every update.
    success_rate: float = 0.0
    last_updated: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True)),
    )
