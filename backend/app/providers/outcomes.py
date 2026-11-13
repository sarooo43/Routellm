"""One honest mapping from an HTTP response to a provider outcome.

Before this module each provider had its own chain of ``if`` statements that
handled 401/403/429/5xx and then gave up:

    if response.status_code != 200:
        return make_error(..., f"unexpected_error: HTTP {response.status_code}", ...)

Two things were wrong with that. The body was discarded, so the provider's own
explanation ("model_not_found", "API key not valid") never reached anyone. And
downstream, every unhandled 4xx was labelled ``auth_failed`` — so a **404 for a
retired model was reported to the user as a bad key**, which sends them to
rotate a key that is perfectly fine.

Rules enforced here, and tested:

*   only **401/403** may say anything about the key;
*   **404** is a targeting problem: the model or the endpoint does not exist.
    The message names the model and the URL host, never the key;
*   **400** is a malformed request — include the provider's message;
*   429 and 5xx keep their own identities;
*   a sanitized slice of the provider body is always kept, so the next person
    debugging this can see what the provider actually said.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

from app.providers.base import make_error, make_success, elapsed_ms

#: How much of the provider body we keep. Raised from 200 to 1000 (T4): the
#: retired-model 404 OBSERVED LIVE carries its full guidance ("...Please
#: update your code to use models/gemini-3.8-flash for the latest features
#: and improve...") and the 200-char cap cut the tail off in logs and UI.
#: 1000 chars still fits a log line; redaction still runs over the text.
BODY_SNIPPET_CHARS = 1000

#: Every outcome this module can produce. Kept in one place so the API's
#: user-facing verdict table can be asserted to cover all of them instead of
#: drifting into "provider_error" for anything new.
OUTCOMES = frozenset(
    {
        "auth_failed",
        "missing_provider_key",
        "model_not_found",
        "endpoint_not_found",
        "not_found",
        "bad_request",
        "rate_limit_exceeded",
        "provider_server_error",
        "provider_unavailable",
        "network_error",
        "timeout",
        "unexpected_status",
    }
)

#: Keys, tokens and secrets as providers echo them back.
_SECRET_PATTERNS = (
    re.compile(r"\b(sk|rk|gsk|hf|AIza)[-_A-Za-z0-9]{6,}\b"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(\"?(api[_-]?key|key|token)\"?\s*[:=]\s*\"?)[^\s\",}]{6,}"),
    re.compile(r"(?i)([?&](key|api_key|apikey)=)[^&\s]+"),
)


def redact(text: str) -> str:
    """Strip anything key-shaped from a provider message.

    Providers do quote keys back in error text (Google's "API key not valid.
    Please pass a valid API key." is the polite version; some include the key).
    A 200-character snippet is worthless if it leaks the credential into a log
    file or a dashboard anyone can screenshot.
    """
    cleaned = text
    for pattern in _SECRET_PATTERNS:
        replacement = r"\1***" if pattern.groups else "***"
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def scrub(text: str, *secrets: Optional[str]) -> str:
    """Redact the EXACT stored key value(s) plus every key-shaped pattern.

    T1: diagnostics copy the provider's words into a response body, a log
    and a clipboard report. The stored key itself rarely matches the
    generic patterns (prefixes vary), so it is removed first, verbatim;
    then redact() sweeps the standard shapes (AIza…, gsk_…, sk-…, Bearer…,
    key=… query echoes).
    """
    cleaned = text or ""
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "***")
    return redact(cleaned)


def snippet(response) -> str:
    """First ~1000 chars of the body, redacted and whitespace-collapsed.

    Redaction runs BEFORE the slice: a credential echoed near the cut point
    would otherwise survive half-redacted.
    """
    try:
        raw = response.text or ""
    except Exception:  # pragma: no cover - exotic transport
        return ""
    collapsed = " ".join(raw.split())
    return redact(collapsed)[:BODY_SNIPPET_CHARS]


def host_of(url: str) -> str:
    """Host only — never the path, which can carry a key in header-less APIs."""
    try:
        return urlsplit(url).netloc or url
    except Exception:  # pragma: no cover
        return url


def classify_status(status_code: int) -> str:
    """Map an HTTP status to our outcome vocabulary.

    Deliberately coarse but honest: the four buckets that mean different things
    to the user must never be merged. ``model_not_found`` and
    ``endpoint_not_found`` are split by the caller (which knows whether it was
    a model call), because "no such model" and "no such route" have different
    fixes.
    """
    if status_code == 400:
        return "bad_request"
    if status_code in (401, 403):
        return "auth_failed"
    if status_code == 404:
        return "not_found"
    if status_code == 429:
        return "rate_limit_exceeded"
    if status_code >= 500:
        return "provider_server_error"
    if 400 <= status_code < 500:
        # 405/415/422 on a provider API means we built the request wrong, which
        # is our bug, not the caller's key.
        return "bad_request"
    return "unexpected_status"


def describe_failure(
    outcome: str,
    *,
    model: str,
    url: str,
    status_code: Optional[int] = None,
    body: str = "",
    provider: str = "",
) -> str:
    """Human sentence for the outcome. Carries model + host, never the key."""
    host = host_of(url)
    # Name the provider AND the host: the provider name is what the user sees
    # everywhere else, and the host is what actually answered (and is the thing
    # that is wrong when the URL is).
    label = f"{provider} ({host})" if provider else host
    detail = f" Provider said: {body}" if body else ""

    if outcome == "auth_failed":
        lowered = body.lower()
        # OBSERVED LIVE (owner's key): 403 PERMISSION_DENIED on every gemini
        # generateContent call while the SAME key's list-models succeeded —
        # so a 403 must never say "invalid key". Only a 401, or Google's
        # literal "API key not valid" body, means the credential is bad.
        if status_code == 401 or "api key not valid" in lowered:
            return (
                f"auth_failed: {label} says the key itself is invalid "
                f"(HTTP {status_code}). Replace the key in Settings — this "
                f"is not a permissions or region problem.{detail}"
            )
        if status_code == 403:
            return (
                f"auth_failed: This key can list models but isn't allowed to "
                f"call this one. Check key restrictions, project access, "
                f"region or billing. ({label}, HTTP {status_code}).{detail}"
            )
        return (
            f"auth_failed: {label} rejected the credential (HTTP {status_code})."
            f"{detail}"
        )
    if outcome == "model_not_found":
        return (
            f"model_not_found: {label} has no model named '{model}' for this key "
            f"(HTTP {status_code}). The URL and your key are fine — the model "
            f"string is wrong, retired, or not enabled for your account."
            f"{detail}"
        )
    if outcome == "endpoint_not_found":
        return (
            f"endpoint_not_found: {label} has nothing at that path (HTTP "
            f"{status_code}). The request URL is wrong, not the key.{detail}"
        )
    if outcome == "bad_request":
        return (
            f"bad_request: {label} rejected the request as malformed (HTTP "
            f"{status_code}). This is a bug in how the request was built, not "
            f"your key.{detail}"
        )
    if outcome == "rate_limit_exceeded":
        return f"rate_limit_exceeded: {label} is rate limiting this key (HTTP 429).{detail}"
    if outcome == "provider_server_error":
        return (
            f"provider_server_error: {label} failed on its side (HTTP "
            f"{status_code}). Nothing is wrong with your key or this machine.{detail}"
        )
    return f"unexpected_status: {label} answered HTTP {status_code}.{detail}"


def error_for_status(
    response,
    *,
    provider: str,
    model: str,
    url: str,
    latency_ms: float,
    model_call: bool = True,
    has_auth_header: bool = True,
):
    """ProviderResponse for a non-200. Chooses model vs endpoint for a 404.

    A 404 is only treated as ``model_not_found`` when the request actually named
    a model and carried a credential; otherwise it is a wrong URL. Guessing here
    is what produced the original misdiagnosis.

    DeepSeek bridge: DeepSeek answers **400** (not 404) with "Model Not Exist"
    for a retired model string. That is still a targeting problem, not a
    malformed request, so when the body says the MODEL does not exist we
    classify it as ``model_not_found`` — the candidate walk needs that signal.
    """
    status_code = response.status_code
    outcome = classify_status(status_code)
    if outcome == "not_found":
        outcome = "model_not_found" if (model_call and model and has_auth_header) else "endpoint_not_found"
    elif (
        outcome == "bad_request"
        and model_call
        and model
        and has_auth_header
        and _body_says_model_missing(response)
    ):
        outcome = "model_not_found"

    body = snippet(response)
    provider_status, provider_reason = _provider_error_identity(response)
    return make_error(
        provider,
        model,
        describe_failure(
            outcome,
            model=model,
            url=url,
            status_code=status_code,
            body=body,
            provider=provider,
        ),
        latency_ms,
        #: Machine-readable outcome for breaker/telemetry decisions.
        outcome=outcome,
        #: T3: the model-status cache honors the provider's own backoff hint.
        retry_after=_retry_after_seconds(response),
        #: T4: the provider's own machine error identity (error.status /
        #: error.reason), surfaced by diagnostics and the explorer.
        provider_status=provider_status,
        provider_reason=provider_reason,
        #: T1: the actual wire status for the diagnostics table.
        http_status=status_code,
    )


def _provider_error_identity(response) -> tuple[Optional[str], Optional[str]]:
    """Google's ``error.status`` / ``error.reason`` from a JSON body.

    OBSERVED LIVE (observation 4): the owner's direct generateContent calls
    answered 403 with the body unseen — so the identity must come from the
    body WHEN we have one and must degrade to (None, None) when we don't.
    Non-JSON bodies (plain text, HTML) simply carry no identity.
    """
    try:
        raw = response.text or ""
    except Exception:  # pragma: no cover - exotic transport
        return None, None
    if not raw.lstrip().startswith("{"):
        return None, None
    import json

    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None, None
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return None, None
    status = err.get("status")
    reason = err.get("reason")
    return (
        status if isinstance(status, str) and status else None,
        reason if isinstance(reason, str) and reason else None,
    )


def _retry_after_seconds(response) -> Optional[float]:
    """Numeric Retry-After header in seconds, or None (HTTP-date ignored)."""
    try:
        raw = response.headers.get("retry-after")
    except Exception:  # pragma: no cover - exotic transport
        return None
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, value)


_MODEL_MISSING_RE = re.compile(
    r"model.{0,40}(not\s*exist|does\s*not\s*exist|not\s*found|not\s*available)"
    r"|invalid\s+model"
    r"|model_not_found",
    re.IGNORECASE,
)


def _body_says_model_missing(response) -> bool:
    """True when a 400 body blames the MODEL string (DeepSeek's wording)."""
    try:
        raw = response.text or ""
    except Exception:  # pragma: no cover - exotic transport
        return False
    return bool(_MODEL_MISSING_RE.search(" ".join(raw.split())[:400]))


def error_for_exception(exc: Exception, *, provider: str, model: str, url: str, latency_ms: float):
    """Network-shaped failures: timeouts, DNS, TLS, connection resets."""
    import httpx

    host = host_of(url)
    if isinstance(exc, httpx.TimeoutException):
        outcome = "timeout"
        message = (
            f"network_error: {host} did not answer within the client timeout. "
            f"Slow network, a blocking proxy, or an overloaded provider."
        )
    elif isinstance(exc, httpx.ConnectError):
        outcome = "network_error"
        message = (
            f"network_error: this machine could not open a connection to {host} "
            f"({type(exc).__name__}). The gateway is working — the outbound "
            f"request failed. Check internet access, a proxy or a firewall."
        )
    else:
        outcome = "network_error"
        message = (
            f"network_error: the request to {host} failed before a response "
            f"({type(exc).__name__})."
        )
    return make_error(provider, model, message, latency_ms, outcome=outcome)


def success_from(response, *, provider: str, model: str, text: str, tokens: int, latency_ms: float):
    return make_success(provider, model, text, tokens, latency_ms)


def elapsed(start: float) -> float:
    return elapsed_ms(start)
