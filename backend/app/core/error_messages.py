"""Turn a raw provider exception into something a human can act on.

The dashboard used to show exactly what the network layer said:

    All providers failed: unexpected_error: ConnectError

That is a class name, not a diagnosis. It tells the reader nothing about
*whose* network failed, whether the key was wrong, or what to do next — and in
practice it sends people to the wrong place (the first field report was
"the backend is not running", when the backend had answered every request with
a 200).

Two rules here:

* **Never claim more than we know.** A ``ConnectError`` means *this machine*
  could not open a connection. It does not mean the provider is down, and it
  says nothing about the user's key. The message has to keep that distinction,
  because a wrong-but-confident diagnosis is worse than a vague one.
* **Always say what to do next.** Every message ends with an action.

The code stays first in the string so it is greppable in logs and stable for
tests: ``network_unreachable: …``. Providers keep emitting their own codes;
this module only rewrites them at the edge, where a person is reading.
"""

from __future__ import annotations

from typing import Optional

#: Codes that mean "the request never reached the provider".
UNREACHABLE_CODES = frozenset(
    {
        "network_unreachable",
        "dns_failure",
        "tls_failure",
        "connection_refused",
        "proxy_blocked",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "TimeoutException",
        "timeout",
        "network_error",
    }
)

#: Codes for a key or account problem (the provider answered, and refused).
AUTH_CODES = frozenset({"auth_failed", "invalid_api_key", "unauthorized", "forbidden"})

#: No key was configured at all — the request never left the process.
MISSING_KEY_CODES = frozenset({"missing_provider_key", "no_api_key", "key_not_configured"})

#: Codes where the provider itself is unhealthy.
PROVIDER_DOWN_CODES = frozenset(
    {"provider_server_error", "server_error", "service_unavailable", "overloaded"}
)

RATE_LIMIT_CODES = frozenset({"rate_limit_exceeded", "rate_limited", "quota_exceeded"})

#: Provider keys and where to manage them.
CONSOLE_URLS = {
    "groq": "https://console.groq.com/keys",
    "gemini": "https://aistudio.google.com/app/apikey",
    "deepseek": "https://platform.deepseek.com/api_keys",
}


def _classify(code: str) -> str:
    """Bucket a provider error code into one of our four families."""
    lowered = code.strip().lower()
    if lowered in {c.lower() for c in UNREACHABLE_CODES}:
        return "unreachable"
    if lowered in {c.lower() for c in MISSING_KEY_CODES}:
        return "missing_key"
    if lowered in {c.lower() for c in AUTH_CODES}:
        return "auth"
    if lowered in {c.lower() for c in PROVIDER_DOWN_CODES}:
        return "provider_down"
    if lowered in {c.lower() for c in RATE_LIMIT_CODES}:
        return "rate_limited"
    if lowered.startswith("unexpected_error: http 5"):
        return "provider_down"
    if lowered.startswith("unexpected_error: http 4"):
        return "auth"
    if lowered.startswith("chaos_injected"):
        return "chaos"
    return "unknown"


def explain(error: Optional[str], provider: Optional[str] = None) -> Optional[str]:
    """Human-readable explanation of a provider failure.

    Returns ``None`` for an empty error. The original code is preserved as the
    prefix, so ``explain("ConnectError")`` reads
    ``network_unreachable: …`` — greppable *and* instructive.
    """
    if not error or not error.strip():
        return None

    raw = error.strip()
    # Providers sometimes already prefix us ("unexpected_error: ConnectError"),
    # but the HTTP status is only legible in the FULL string, so it is matched
    # before the prefix is stripped.
    if raw.lower().startswith("unexpected_error: http 5"):
        family, bare = "provider_down", raw
    elif raw.lower().startswith("unexpected_error: http 4"):
        family, bare = "auth", raw
    else:
        bare = raw.split(":", 1)[-1].strip() if raw.lower().startswith("unexpected_error") else raw
        family = _classify(bare)
    name = provider or "the provider"
    console = CONSOLE_URLS.get((provider or "").lower())

    if family == "unreachable":
        return (
            f"network_unreachable: this machine could not open a connection to "
            f"{name} ({bare}). The gateway is working — it is the outbound "
            f"request that failed. Check internet access, a corporate proxy or "
            f"VPN, or a firewall rule, then retry."
        )

    if family == "missing_key":
        where = f" at {console}" if console else ""
        return (
            f"missing_provider_key: no API key is configured for {name}, so the "
            f"request was never sent. Get a free key{where} and paste it on the "
            f"Providers page."
        )

    if family == "auth":
        where = f" Check the key at {console}," if console else ""
        return (
            f"auth_failed: {name} rejected the request as unauthenticated "
            f"({bare}).{where} then paste it again on the Providers page."
        )

    if family == "provider_down":
        return (
            f"provider_unavailable: {name} answered with an error of its own "
            f"({bare}). Nothing is wrong with this machine or your key — retry "
            f"shortly, or let automatic fallback use another provider."
        )

    if family == "rate_limited":
        return (
            f"rate_limited: {name} refused the request because a limit was "
            f"reached ({bare}). Wait for the window to reset, or add another "
            f"provider so fallback can take over."
        )

    if family == "chaos":
        return (
            f"chaos_injected: this failure was simulated on purpose by chaos "
            f"mode, not produced by {name}. Turn chaos mode off to see the real "
            f"behaviour."
        )

    return (
        f"provider_error: {name} failed with an unexpected error ({raw}). "
        f"The full detail is in the server log; retry, and check the Providers "
        f"page if it keeps happening."
    )


#: The executor's "everything failed" summary code. It says nothing a reader
#: can act on, so the edge replaces it with the first real failure.
GENERIC_FAILURE_CODES = frozenset({"all_providers_failed", "forced_provider_failed"})


def explain_route_failure(
    error: Optional[str], attempts: list
) -> Optional[str]:
    """Best available diagnosis for a whole route.

    ``result.error`` is often the generic ``all_providers_failed`` sentinel
    while the useful code sits on the individual attempts, so the specific
    failure wins. Accepts ``AttemptLog`` objects or their dumped dicts.
    """
    def _field(attempt, name, default=None):
        return attempt.get(name, default) if isinstance(attempt, dict) else getattr(
            attempt, name, default
        )

    failures = [a for a in attempts if not _field(a, "success", False)]
    first = failures[0] if failures else None

    specific = _field(first, "error") if first else None
    source = specific
    if not source or (error and error.strip() not in GENERIC_FAILURE_CODES):
        source = error or source

    provider = _field(first, "provider_name") if first else None
    explained = explain(source, provider)
    if explained is None:
        return None
    if len(failures) > 1:
        return f"{explained} All {len(failures)} attempted providers failed."
    return explained


def summarize_attempt_chain(attempts: list[dict]) -> Optional[str]:
    """One sentence for the whole chain, or ``None`` when anything succeeded.

    Falls back to the first real failure's explanation so a five-provider chain
    does not print five paragraphs.
    """
    if not attempts:
        return None
    failures = [a for a in attempts if not a.get("success")]
    if not failures:
        return None
    # A chain that eventually succeeded is not a failure story: the caller
    # should only ask for a summary when nothing worked.
    if any(a.get("success") for a in attempts):
        return None
    first = failures[0]
    explained = explain(first.get("error"), first.get("provider_name"))
    if explained is None:
        return None
    if len(failures) == 1:
        return explained
    return f"{explained} All {len(failures)} attempted providers failed the same way."
