"""A credential must not appear in a log line, an error string or a response.

Swept rather than spot-checked: the same secret is pushed through every path
that can produce text a human or a log file will read — provider errors, the
discovery cache, the connection test, the fallback chain, and the exceptions
module — and then the *whole* concatenation is searched. One missed ``repr`` in
a warning line is enough to leak a key into a support ticket.
"""

import json

import httpx
import pytest

from app.providers.base import ProviderResponse
from app.providers.outcomes import error_for_exception, error_for_status, redact, snippet

SECRETS = {
    "groq": "gsk_LEAKCANARY1234567890abcdef",
    "gemini": "AIzaLEAKCANARY1234567890abcdef",
    "deepseek": "sk-LEAKCANARY1234567890abcdef",
}


def _assert_clean(text: str) -> None:
    for provider, secret in SECRETS.items():
        assert secret not in text, f"{provider} key leaked"
        # The middle of a key is just as damaging as the whole of it.
        assert secret[8:20] not in text, f"{provider} key fragment leaked"


@pytest.mark.parametrize("provider", list(SECRETS))
def test_provider_echo_in_an_error_body_is_redacted(provider):
    secret = SECRETS[provider]
    request = httpx.Request("POST", "https://api.example.test/v1/chat")
    response = httpx.Response(
        401,
        text=json.dumps({"error": {"message": f"Incorrect API key provided: {secret}"}}),
        request=request,
    )
    resp = error_for_status(
        response, provider=provider, model="m", url=str(request.url), latency_ms=1.0
    )
    _assert_clean(resp.error)


def test_query_string_echo_is_redacted(provider="gemini"):
    """Gemini accepts ?key=…, and its errors quote the URL back."""
    secret = SECRETS[provider]
    response = httpx.Response(
        400,
        text=f"API key not valid: {secret}. Please pass a valid API key.",
        request=httpx.Request("POST", f"https://x.test/v1beta/models/m:generateContent?key={secret}"),
    )
    resp = error_for_status(
        response, provider=provider, model="m", url=f"https://x.test/models/m?key={secret}", latency_ms=1
    )
    _assert_clean(resp.error)


def test_an_exception_message_carrying_a_key_is_redacted():
    secret = SECRETS["groq"]
    exc = RuntimeError(f"connect failed for Authorization: Bearer {secret}")
    resp = error_for_exception(
        exc, provider="groq", model="m", url="https://api.groq.com/x", latency_ms=1.0
    )
    _assert_clean(resp.error)


def test_bearer_and_basic_headers_are_redacted():
    secret = SECRETS["deepseek"]
    cleaned = redact(f"Authorization: Bearer {secret}\nX-Api-Key: {secret}")
    _assert_clean(cleaned)


def test_snippet_of_a_huge_body_stays_bounded_and_clean():
    secret = SECRETS["gemini"]
    response = httpx.Response(
        400, text=f"{'x' * 4000} {secret} {'y' * 4000}", request=httpx.Request("GET", "https://x.test/")
    )
    out = snippet(response)
    # T3/T4 raised the cap from 200 to 1000 chars (the retired-model 404
    # body OBSERVED LIVE names its replacement in the tail). The bound here
    # tracks the constant so the two can't drift apart silently.
    from app.providers.outcomes import BODY_SNIPPET_CHARS

    assert len(out) <= BODY_SNIPPET_CHARS
    assert len(out) > 200  # the cap genuinely moved
    _assert_clean(out)


def test_provider_response_repr_does_not_include_a_key():
    """A stray f-string over the response object must still be safe."""
    resp = ProviderResponse(
        success=False,
        error="auth_failed: nope",
        provider_name="groq",
        model_name="llama-3.1-8b-instant",
        outcome="auth_failed",
    )
    _assert_clean(repr(resp))
    _assert_clean(resp.model_dump_json())


async def test_the_connection_test_response_never_contains_the_key(respx_mock):
    from app.api import provider_test_routes
    from app.providers.model_registry import model_catalog

    secret = SECRETS["groq"]
    model_catalog.clear()
    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "llama-3.1-8b-instant"}]})
    )
    respx_mock.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(401, text=f'{{"error":"bad key {secret}"}}')
    )

    class _Row:
        id = 1

    # Bypass stored-key lookup by patching the "effective key" resolution only.
    import app.core.provider_key_manager as pkm

    original = pkm.get_provider_key

    async def _stored(_session, _id, _provider):
        return secret

    pkm.get_provider_key = _stored
    try:
        # No db session needed: get_provider_key is replaced above.
        result = await provider_test_routes._probe("groq", "", None, _Row())  # type: ignore[arg-type]
    finally:
        pkm.get_provider_key = original

    _assert_clean(result.model_dump_json())
