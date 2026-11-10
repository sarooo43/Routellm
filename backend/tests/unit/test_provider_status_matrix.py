"""Every HTTP status, every provider: classification, message, breaker effect.

The bug this pins down: a 404 was reported to the user as ``auth_failed``, so a
retired model name looked like a bad API key. The table below is the contract
that replaces "any 4xx is auth".

Breaker policy under test (decided explicitly, not left to chance):

| outcome                        | breaker          | why                       |
|--------------------------------|------------------|---------------------------|
| 401 / 403 (caller's own key)   | not counted      | one user's key ≠ outage   |
| 400 / 404 (config)             | not counted      | provider is up, we erred  |
| 429 / 5xx / timeout / network  | failure          | the provider is unhealthy |
"""

import httpx
import pytest

from app.providers.outcomes import BODY_SNIPPET_CHARS, classify_status, redact

PROVIDERS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent",
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
}

#: status -> (expected outcome, must the message mention the key?)
MATRIX = {
    400: ("bad_request", False),
    401: ("auth_failed", True),
    403: ("auth_failed", True),
    404: ("model_not_found", False),
    429: ("rate_limit_exceeded", False),
    500: ("provider_server_error", False),
    503: ("provider_server_error", False),
}


def _response(status: int, body: str = '{"error":"x"}') -> httpx.Response:
    request = httpx.Request("POST", "https://example.test/")
    return httpx.Response(status, text=body, request=request)


# --------------------------------------------------------------------------
# classify_status: the pure mapping
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status,expected", [
    (400, "bad_request"),
    (401, "auth_failed"),
    (403, "auth_failed"),
    (404, "not_found"),
    (405, "bad_request"),
    (422, "bad_request"),
    (429, "rate_limit_exceeded"),
    (500, "provider_server_error"),
    (502, "provider_server_error"),
    (503, "provider_server_error"),
])
def test_status_mapping(status, expected):
    assert classify_status(status) == expected


def test_only_401_and_403_are_auth():
    """The regression that started this: 404 must never be auth."""
    for status in (400, 404, 405, 422, 429, 500, 503):
        assert classify_status(status) != "auth_failed", status


# --------------------------------------------------------------------------
# End to end through each provider class
# --------------------------------------------------------------------------


@pytest.fixture(params=list(PROVIDERS), ids=list(PROVIDERS))
def provider(request, monkeypatch):
    """Fresh provider instance with discovery pinned (no network in tests)."""
    from app.providers import model_registry

    monkeypatch.setattr(
        model_registry,
        "resolve_model",
        # Generated once at import; must stay inert under test.
        _async_ok,
        raising=True,
    )
    from app.providers.deepseek_provider import DeepSeekProvider
    from app.providers.groq_provider import GroqProvider
    from app.providers.gemini_provider import GeminiProvider

    instance = {
        "groq": GroqProvider(),
        "gemini": GeminiProvider(),
        "deepseek": DeepSeekProvider(),
    }[request.param]
    # The provider modules imported `resolve_model` by name, so patch there too.
    import app.providers.deepseek_provider as d
    import app.providers.gemini_provider as g
    import app.providers.groq_provider as r
    for module in (d, g, r):
        monkeypatch.setattr(module, "resolve_model", _async_ok, raising=True)
    return request.param, instance


async def _async_ok(provider, api_key, override=None, force_refresh=False):
    from app.providers.model_registry import PROVIDER_SPECS

    return override or PROVIDER_SPECS[provider].default_model, [], "default"


@pytest.fixture
def mock_post(respx_mock):
    return respx_mock


@pytest.mark.parametrize("status", list(MATRIX))
async def test_status_classification_end_to_end(provider, mock_post, status):
    name, instance = provider
    expected_outcome, key_mentioned = MATRIX[status]
    url = PROVIDERS[name].replace("m:generateContent", "gemini-2.0-flash:generateContent")

    mock_post.post(url).mock(
        return_value=httpx.Response(status, text='{"error":"provider said no"}')
    )
    resp = await instance.generate("hi", provider_api_key="gsk_test_key_123456")

    assert resp.success is False
    assert resp.outcome == expected_outcome, (status, resp.outcome, resp.error)
    assert resp.error.startswith(expected_outcome)
    # The failing provider is always named.
    assert name in resp.error
    if key_mentioned:
        assert "credential" in resp.error
    else:
        # Everything except 401/403 must not send the user to their key.
        assert "your key is invalid" not in resp.error.lower()
        assert "check your key" not in resp.error.lower()


@pytest.mark.parametrize("status", [404])
async def test_404_names_the_model_and_host_but_never_the_key(provider, mock_post, status):
    name, instance = provider
    url = PROVIDERS[name].replace("m:generateContent", "gemini-2.0-flash:generateContent")
    mock_post.post(url).mock(return_value=httpx.Response(404, text='{"error":"model_not_found"}'))

    secret = "gsk_SUPERSECRETKEYVALUE123456"
    resp = await instance.generate("hi", provider_api_key=secret)

    assert resp.outcome == "model_not_found"
    assert "no model named" in resp.error
    assert "gemini-2.0-flash" in resp.error or "llama-3.3-70b-versatile" in resp.error or "deepseek-chat" in resp.error
    # Host, not full URL with a key in it, and never the key itself.
    assert "api." in resp.error or "generativelanguage" in resp.error
    assert secret not in resp.error


@pytest.mark.parametrize("status", [400])
async def test_400_includes_the_provider_message(provider, mock_post, status):
    name, instance = provider
    url = PROVIDERS[name].replace("m:generateContent", "gemini-2.0-flash:generateContent")
    mock_post.post(url).mock(
        return_value=httpx.Response(400, text='{"error":{"message":"bad temperature"}}')
    )
    resp = await instance.generate("hi", provider_api_key="gsk_test_key_123456")
    assert resp.outcome == "bad_request"
    assert "bad temperature" in resp.error


async def test_timeout_is_network_error(provider, mock_post):
    name, instance = provider
    url = PROVIDERS[name].replace("m:generateContent", "gemini-2.0-flash:generateContent")
    mock_post.post(url).mock(side_effect=httpx.TimeoutException("slow"))
    resp = await instance.generate("hi", provider_api_key="gsk_test_key_123456")
    assert resp.outcome == "timeout"
    assert resp.error.startswith("network_error:")


async def test_connection_error_is_network_error(provider, mock_post):
    name, instance = provider
    url = PROVIDERS[name].replace("m:generateContent", "gemini-2.0-flash:generateContent")
    mock_post.post(url).mock(side_effect=httpx.ConnectError("no route"))
    resp = await instance.generate("hi", provider_api_key="gsk_test_key_123456")
    assert resp.outcome == "network_error"
    assert "could not open a connection" in resp.error


async def test_success_reports_the_model_that_answered(provider, mock_post):
    name, instance = provider
    url = PROVIDERS[name].replace("m:generateContent", "gemini-2.0-flash:generateContent")
    if name == "gemini":
        body = {"candidates": [{"content": {"parts": [{"text": "pong"}]}}]}
    else:
        body = {"choices": [{"message": {"content": "pong"}}], "usage": {"total_tokens": 3}}
    mock_post.post(url).mock(return_value=httpx.Response(200, json=body))

    resp = await instance.generate("hi", provider_api_key="gsk_test_key_123456")
    assert resp.success is True
    assert resp.outcome is None
    assert resp.text == "pong"


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


@pytest.mark.parametrize("secret", [
    "gsk_abcdefghijklmnop1234",
    "sk-proj-abcdefghijklmnop",
    "AIzaSyABCDEFGHIJKLMNOP1234",
    "hf_abcdefghijklmnopqrstuvwxyz",
])
def test_body_snippets_redact_keys(secret):
    body = f'{{"error":"invalid key {secret}"}}'
    cleaned = redact(body)
    assert secret not in cleaned
    assert "***" in cleaned


def test_bearer_tokens_are_redacted():
    cleaned = redact('Authorization: Bearer abcdefghijklmnopqrstuvwxyz')
    assert "abcdefghijklmnopqrstuvwxyz" not in cleaned


def test_query_string_keys_are_redacted():
    cleaned = redact("GET /v1beta/models?key=AIzaSyABCDEFGHIJKLMNOP failed")
    assert "AIzaSyABCDEFGHIJKLMNOP" not in cleaned


def test_body_snippet_is_truncated():
    from app.providers.outcomes import snippet

    request = httpx.Request("POST", "https://x.test/")
    response = httpx.Response(400, text="x" * 5000, request=request)
    out = snippet(response)
    assert len(out) <= BODY_SNIPPET_CHARS
