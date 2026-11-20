"""Defense-in-depth masking for log/error strings.

Nothing in the app intentionally logs a secret, but infra client libraries
can embed credentials in connection-error text (and we do want the *useful*
part of that text in our warnings).

STATUS (audited 2026-11): **not wired up.** ``mask_secrets`` has no caller, no
test, and nothing else in the repository imports this module. The masking that
IS live happens in three other places, each tested:

  * ``app.providers.outcomes.redact`` — key-shaped text in provider messages.
  * ``app.api.provider_keys_routes._scrub`` — the diagnostics report and its
    Markdown.
  * ``app.utils.logger.mask_database_url`` — the DSN in structured logs.

This module is kept rather than deleted because wiring it into the
infrastructure-error log path (``main._sqlalchemy_error_handler``,
``ConnectionRefusedError``) is a behaviour change that belongs to the owner:
it would also mask text that is currently useful when debugging a connection.
The earlier version of this docstring claimed every log line went through
``mask_secrets()``; that was not true, and the claim is corrected here rather
than by quietly deleting a security-shaped file.

OBSERVED, so nobody re-opens this: a real server with a deliberately wrong
Postgres password answers 503 and its log does NOT contain the password.
SQLAlchemy/asyncpg mask the DSN themselves before the message reaches
``str(exc)``, which is what the handler logs.
"""

import re

from typing import Optional

# user:password@ inside ANY scheme URL -> user:****@
_CRED_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)([^:/@\s]+):([^@\s/]+)@")

# Token-shaped prefixes: scheme marker + a long opaque tail.
_TOKEN_PATTERNS = (
    re.compile(r"\b(rk_[A-Za-z0-9_\-]{4,})"),  # Routtener keys (prefix unchanged)
    re.compile(r"\b(hf_[A-Za-z0-9_\-]{4,})"),  # HuggingFace tokens
    re.compile(r"\b(gsk_[A-Za-z0-9_\-]{4,})"),  # Groq
    re.compile(r"\b(AIza[A-Za-z0-9_\-]{10,})"),  # Google
    re.compile(r"\b(sk-[A-Za-z0-9][A-Za-z0-9_\-]{6,})"),  # DeepSeek / OpenAI
)


def _mask_token(match: re.Match) -> str:
    token = match.group(1)
    return f"{token[:min(4, len(token))]}****"


def mask_secrets(text: Optional[str]) -> str:
    """Mask URL credentials and token-shaped secrets in arbitrary text."""
    if not text:
        return ""
    masked = _CRED_IN_URL_RE.sub(r"\1\2:****@", text)
    for pattern in _TOKEN_PATTERNS:
        masked = pattern.sub(_mask_token, masked)
    return masked
