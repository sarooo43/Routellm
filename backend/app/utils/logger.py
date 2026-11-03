import logging
import os
import sys

from typing import Optional

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
        # stdout, not a file: containers (Hugging Face Spaces, Docker) treat
        # stdout as the log sink, and the app directory may be read-only.
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )


configure_logging()

#: Environment variables whose values must never be printed, even when they
#: are partially shown. Used by :func:`redact_env` for startup diagnostics.
_SECRET_ENV_NAMES = {
    "DATABASE_URL",
    "REDIS_URL",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "PROVIDER_KEY_ENCRYPTION_KEY",
    "HF_TOKEN",
    "ENCRYPTION_KEY",
}

#: Suffixes that mark an env var as sensitive regardless of its full name.
_SECRET_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_KEY")


def is_secret_env(name: str) -> bool:
    """True when an environment variable's value must be redacted/logged as set."""
    upper = name.upper()
    if upper in _SECRET_ENV_NAMES:
        return True
    return upper.endswith(_SECRET_ENV_SUFFIXES)


def redact_env(name: str) -> Optional[str]:
    """Return ``"<set>"`` for a secret env var and its value for anything else.

    Deliberate: the caller decides whether the *presence* of a secret is worth
    logging (it usually is — "is GROQ_API_KEY configured?" is the question the
    startup log has to answer), but the value never is.
    """
    value = os.environ.get(name)
    if value is None:
        return None
    if is_secret_env(name):
        return "<set>"
    return value


def get_logger(name: str) -> FilteringBoundLogger:
    return structlog.get_logger(name)
