from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, DateTime

from sqlmodel import Field, SQLModel

from app.models.task_log import utcnow


class Feedback(SQLModel, table=True):
    """Human thumbs-up/down on a single routed response.

    Ties back to the TaskLog via ``request_id`` (which the executor minted
    and the /route response returned). One request_id gets at most one
    rating — enforced in the endpoint (409 Conflict), not silently
    overwritten.
    """

    __tablename__ = "feedback"

    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: str = Field(index=True)
    provider_name: str
    task_type: str
    rating: str  # "positive" or "negative"
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), index=True),
    )
