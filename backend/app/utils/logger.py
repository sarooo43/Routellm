import logging

import structlog
from structlog.typing import FilteringBoundLogger

from app.config import settings

_VALID_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}


def configure_logging() -> None:
    level_name = settings.LOG_LEVEL.upper()
    if level_name not in _VALID_LEVELS:
        level_name = "INFO"

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level_name, logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


configure_logging()


def get_logger(name: str) -> FilteringBoundLogger:
    return structlog.get_logger(name)
