"""Database models (Phase 6).

Importing this package registers every table on ``SQLModel.metadata`` so
``init_db()`` / Alembic can create them all.
"""

from app.models.api_key import ApiKeyDB  # noqa: E402
from app.models.feedback import Feedback
from app.models.model_config import ModelConfigDB  # noqa: E402
from app.models.model_stats import ModelStatsDB  # noqa: E402
from app.models.provider_key import ProviderKeyDB  # noqa: E402
from app.models.task_log import TaskLog  # noqa: E402
from app.models.usage_tracking import UsageTracking  # noqa: E402

__all__ = [
    "ApiKeyDB",
    "Feedback",
    "ModelConfigDB",
    "ModelStatsDB",
    "ProviderKeyDB",
    "TaskLog",
    "UsageTracking",
]
