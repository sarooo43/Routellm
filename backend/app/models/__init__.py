"""Database models (Phase 6).

Importing this package registers every table on ``SQLModel.metadata`` so
``init_db()`` / Alembic can create them all.
"""

from app.models.model_config import ModelConfigDB  # noqa: E402
from app.models.model_stats import ModelStatsDB  # noqa: E402
from app.models.task_log import TaskLog  # noqa: E402
from app.models.usage_tracking import UsageTracking  # noqa: E402

__all__ = ["ModelConfigDB", "ModelStatsDB", "TaskLog", "UsageTracking"]
