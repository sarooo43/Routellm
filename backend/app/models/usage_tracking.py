from datetime import date as DateType
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class UsageTracking(SQLModel, table=True):
    __tablename__ = "usage_tracking"
    __table_args__ = (
        UniqueConstraint("provider_name", "date"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_name: str = Field(index=True)
    date: DateType = Field(index=True)
    requests_count: int = 0
    tokens_used: int = 0
