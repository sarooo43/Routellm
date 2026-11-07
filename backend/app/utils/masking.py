"""Defense-in-depth masking for log/error strings.

Nothing in the app intentionally logs a secret, but infra client libraries
can embed credentials in connection-error text (and we do want the *useful*
part of that text in our warnings). Every string that reaches a log line
and might contain infrastructure error detail goes through mask_secrets().
"""

import re

from typing import Optional

# user:password@ inside ANY scheme URL -> user:****@
_CRED_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)([^:/@\s]+):([^@\s/]+)@")

# Token-shaped prefixes: scheme marker + a long opaque tail.
_TOKEN_PATTERNS = (
    re.compile(r"\b(rk_[A-Za-z0-9_\-]{4,})"),  # RouteLLM keys
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
