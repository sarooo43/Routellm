from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Column, DateTime
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskLog(SQLModel, table=True):
    __tablename__ = "task_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: str = Field(index=True)  # UUID minted per request in the executor
    task_type: str
    selected_model: Optional[str] = (
        None  # the model that ultimately succeeded; null if all failed
    )
    confidence_score: float = 0.0
    status: str  # "success" or "failed"
    latency_ms: float
    tokens_used: int = 0
    error_message: Optional[str] = None
    # Native JSON column (works on Postgres in production and SQLite in
    # tests). Stores the full list of AttemptLog dicts for the dashboard.
    fallback_chain: list = Field(
        default_factory=list,
        sa_column=Column(JSON),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), index=True),
    )
