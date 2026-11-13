"""T4: error surface and key transport.

Fixtures are labelled by their provenance:

- OBSERVED LIVE, truncated — text the owner's Codespace actually produced
  (our old 200-char log cap cut the tail, which is exactly why the cap is
  now 1000).
- status only, body unseen — the owner's direct curl calls answered 403
  with a body they did not capture; the code must degrade gracefully to
  (None, None) identity and still produce the right sentence.
- DOCUMENTED — Google's JSON error envelope shape (error.code / message /
  status / reason).
"""

import json

import httpx
import respx

from app.providers.base import elapsed_ms
from app.providers.gemini_provider import GeminiProvider
from app.providers.model_registry import key_transport, spec_for
from app.providers.outcomes import (
    BODY_SNIPPET_CHARS,
    describe_failure,
    error_for_status,
)

GEMINI_SECRET = "AIzaSyDUMMY_transport_secret_123"

# ---------------------------------------------------------------------------
# OBSERVED LIVE, truncated (observation 3): the retired-model 404.
# The message TEXT is what the owner's run produced (our old cap cut it at
# "...and improve"); the JSON wrapper is Google's DOCUMENTED envelope.
# ---------------------------------------------------------------------------

OBSERVED_LIVE_404_MESSAGE = (
    "This model models/gemini-2.5-flash is no longer available to new users. "
    "Please update your code to use models/gemini-3.8-flash for the latest "
    "features and improve"
)


def _observed_404_response() -> httpx.Response:
    return httpx.Response(
        404,
        text=json.dumps(
            {
                "error": {
                    "code": 404,
                    "message": OBSERVED_LIVE_404_MESSAGE,
                    "status": "NOT_FOUND",
                }
            }
        ),
        request=httpx.Request(
            "POST",
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        ),
    )


# OBSERVED LIVE, status only, body unseen (observation 4): 403 on direct
# generateContent with the same key; the owner did not capture the body.


def _status_only_403() -> httpx.Response:
    return httpx.Response(
        403,
        text="",
        request=httpx.Request(
            "POST",
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent",
        ),
    )


def _url(model: str) -> str:
    return spec_for("gemini").chat_url(model)


# ---------------------------------------------------------------------------
# 404: full body kept, replacement hint reachable
# ---------------------------------------------------------------------------


def test_observed_live_404_keeps_the_replacement_hint_in_the_message():
    resp = error_for_status(
        _observed_404_response(),
        provider="gemini",
        model="gemini-2.5-flash",
        url=_url("gemini-2.5-flash"),
        latency_ms=1.0,
    )
    assert resp.outcome == "model_not_found"
    # The tail that the old 200-char cap cut off is inside the 1000-char cap.
    assert "update your code to use models/gemini-3.8-flash" in resp.error
    assert len(resp.error) <= BODY_SNIPPET_CHARS + 400  # message framing + body
    # Never blames the key for a retired model.
    assert "invalid" not in resp.error.lower()


def test_observed_live_404_carries_the_provider_status():
    resp = error_for_status(
        _observed_404_response(),
        provider="gemini",
        model="gemini-2.5-flash",
        url=_url("gemini-2.5-flash"),
        latency_ms=1.0,
    )
    assert resp.provider_status == "NOT_FOUND"
    assert resp.provider_reason is None  # the fixture has no reason field


# ---------------------------------------------------------------------------
# 403: status only, body unseen (observation 4)
# ---------------------------------------------------------------------------


def test_observed_live_403_status_only_body_unseen_still_says_the_right_thing():
    resp = error_for_status(
        _status_only_403(),
        provider="gemini",
        model="gemini-3.8-flash",
        url=_url("gemini-3.8-flash"),
        latency_ms=1.0,
    )
    assert resp.outcome == "auth_failed"
    assert resp.provider_status is None and resp.provider_reason is None
    # The owner's exact 403 sentence, even with no body to parse.
    assert (
        "This key can list models but isn't allowed to call this one. "
        "Check key restrictions, project access, region or billing."
    ) in resp.error
    # A 403 NEVER claims the key is invalid — the same key's list call worked.
    assert "invalid" not in resp.error.lower()
    assert "API key not valid" not in resp.error


def test_observed_live_403_with_google_json_body_exposes_status_and_reason():
    """DOCUMENTED envelope: error.status / error.reason are surfaced."""
    response = httpx.Response(
        403,
        text=json.dumps(
            {
                "error": {
                    "code": 403,
                    "message": "PERMISSION_DENIED",
                    "status": "PERMISSION_DENIED",
                    "reason": "CONSUMER_INVALID",
                }
            }
        ),
        request=httpx.Request("POST", _url("gemini-3.8-flash")),
    )
    resp = error_for_status(
        response,
        provider="gemini",
        model="gemini-3.8-flash",
        url=_url("gemini-3.8-flash"),
        latency_ms=1.0,
    )
    assert resp.provider_status == "PERMISSION_DENIED"
    assert resp.provider_reason == "CONSUMER_INVALID"
    assert (
        "Check key restrictions, project access, region or billing." in resp.error
    )


# ---------------------------------------------------------------------------
# Invalid key: only 401, or Google's literal body wording
# ---------------------------------------------------------------------------


def test_401_says_the_key_itself_is_invalid():
    response = httpx.Response(
        401,
        text=json.dumps({"error": {"code": 401, "status": "UNAUTHENTICATED"}}),
        request=httpx.Request("POST", _url("gemini-2.5-flash")),
    )
    resp = error_for_status(
        response,
        provider="gemini",
        model="gemini-2.5-flash",
        url=_url("gemini-2.5-flash"),
        latency_ms=1.0,
    )
    assert resp.outcome == "auth_failed"
    assert "invalid" in resp.error.lower()
    assert "Replace the key" in resp.error


def test_api_key_not_valid_body_says_invalid_even_on_403():
    response = httpx.Response(
        403,
        text="API key not valid. Please pass a valid API key.",
        request=httpx.Request("POST", _url("gemini-2.5-flash")),
    )
    resp = error_for_status(
        response,
        provider="gemini",
        model="gemini-2.5-flash",
        url=_url("gemini-2.5-flash"),
        latency_ms=1.0,
    )
    assert resp.outcome == "auth_failed"
    assert "invalid" in resp.error.lower()


def test_the_403_sentence_is_stable_in_describe_failure():
    text = describe_failure(
        "auth_failed",
        model="gemini-3.8-flash",
        url=_url("gemini-3.8-flash"),
        status_code=403,
        body="",
        provider="gemini",
    )
    assert "isn't allowed to call this one" in text
    assert "invalid" not in text.lower()


# ---------------------------------------------------------------------------
# Key transport: header, never the URL query
# ---------------------------------------------------------------------------


def test_key_transport_report_says_header_for_every_provider():
    assert "x-goog-api-key" in key_transport("gemini")
    assert "query" in key_transport("gemini")  # explicitly negated
    for provider in ("groq", "deepseek"):
        assert "Authorization" in key_transport(provider)


def test_the_gemini_chat_url_never_carries_a_key_parameter():
    url = _url("gemini-3.8-flash")
    assert "key=" not in url
    assert "api_key=" not in url


def test_a_key_bearing_url_never_reaches_the_error_text():
    """Even if a caller passes a key-bearing URL, only the host survives."""
    url = f"{_url('gemini-2.5-flash')}?key={GEMINI_SECRET}"
    text = describe_failure(
        "model_not_found",
        model="gemini-2.5-flash",
        url=url,
        status_code=404,
        body="",
        provider="gemini",
    )
    assert "key=" not in text
    assert GEMINI_SECRET not in text
    assert GEMINI_SECRET[8:20] not in text  # not even a fragment


@respx.mock
async def test_gemini_provider_sends_the_key_in_the_header_never_the_query():
    """The wire-level pin: header present, URL clean, query params absent."""
    route = respx.post(_url("gemini-3.8-flash")).mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": "hello"}]}}
                ],
                "usageMetadata": {"totalTokenCount": 5},
            },
        )
    )
    provider = GeminiProvider()
    resp = await provider.generate(
        "hi", provider_api_key=GEMINI_SECRET, model="gemini-3.8-flash"
    )
    assert resp.success
    request = route.calls[0].request
    assert request.headers["x-goog-api-key"] == GEMINI_SECRET
    assert str(request.url) == _url("gemini-3.8-flash")
    assert "key=" not in str(request.url)
