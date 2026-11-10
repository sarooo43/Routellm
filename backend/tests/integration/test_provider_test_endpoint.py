"""The Providers-page "Test connection" button, against a mocked wire.

The button must be incapable of disagreeing with routing, and it must never be
able to say "your key is wrong" for a problem that is not the key. Both are
tested here on the HTTP boundary, not by inspecting implementation details.

The historical failure this replaces: the page showed a green badge whenever a
row existed in the database, so a key that 404'd on every real prompt looked
perfectly healthy.
"""

import httpx
import pytest

from app.api import provider_test_routes
from app.providers.model_registry import model_catalog

GROQ_CHAT = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS = "https://api.groq.com/openai/v1/models"

SECRET = "gsk_TESTKEYFORTESTS1234567890"

GROQ_MODEL_LIST = {
    "data": [
        {"id": "whisper-large-v3"},
        {"id": "llama-3.1-8b-instant"},
    ]
}
GROQ_OK = {
    "choices": [{"message": {"content": "pong"}}],
    "usage": {"total_tokens": 1},
}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Fresh model cache, and a server-side key present for every test.

    Autouse so no test can accidentally pass by *not* configuring a key (that
    would take the not_configured branch and assert nothing).
    """
    model_catalog.clear()
    monkeypatch.setattr("app.config.settings.GROQ_API_KEY", SECRET, raising=False)
    yield
    model_catalog.clear()


def _models(respx_mock, status=200, body=None):
    return respx_mock.get(GROQ_MODELS).mock(
        return_value=httpx.Response(status, json=body if body is not None else GROQ_MODEL_LIST)
    )


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


async def test_valid_key_reports_the_model_and_latency(respx_mock, db_session):
    _models(respx_mock)
    respx_mock.post(GROQ_CHAT).mock(return_value=httpx.Response(200, json=GROQ_OK))

    result = await provider_test_routes._probe("groq", "", db_session, _row())

    assert result.verdict == "valid"
    assert result.key_valid is True
    assert result.model == "llama-3.1-8b-instant"
    assert result.latency_ms is not None
    assert result.model_source in {"discovered", "cache"}
    assert "whisper-large-v3" not in result.available_models


# --------------------------------------------------------------------------
# The classification the user actually complained about
# --------------------------------------------------------------------------


async def test_404_is_model_not_found_not_a_key_problem(respx_mock, db_session):
    """THE regression. A retired model must never read as a rejected key."""
    _models(respx_mock)
    respx_mock.post(GROQ_CHAT).mock(
        return_value=httpx.Response(404, json={"error": {"message": "model_not_found"}})
    )

    result = await provider_test_routes._probe("groq", "", db_session, _row())

    assert result.verdict == "model_not_found"
    assert result.key_valid is False
    low = (result.error or "").lower()
    # No blame on the key at all, and an explicit reassurance that it is fine.
    assert "check your key" not in low
    assert "invalid" not in low
    assert "your key are fine" in low
    assert "has no model named" in low
    assert "api.groq.com" in low


async def test_auth_failure_is_the_only_thing_that_blames_the_key(respx_mock, db_session):
    _models(respx_mock, status=200)
    respx_mock.post(GROQ_CHAT).mock(
        return_value=httpx.Response(401, json={"error": "invalid api key"})
    )
    result = await provider_test_routes._probe("groq", "", db_session, _row())
    assert result.verdict == "auth_failed"
    assert result.key_valid is False
    assert "credential" in (result.error or "")


@pytest.mark.parametrize("status,verdict", [
    (429, "rate_limited"),
    (400, "bad_request"),
    (500, "provider_error"),
    (503, "provider_error"),
])
async def test_each_status_maps_to_its_own_verdict(respx_mock, db_session, status, verdict):
    _models(respx_mock)
    respx_mock.post(GROQ_CHAT).mock(return_value=httpx.Response(status, json={"error": "x"}))
    result = await provider_test_routes._probe("groq", "", db_session, _row())
    assert result.verdict == verdict
    assert result.key_valid is False


async def test_timeout_is_network_error(respx_mock, db_session):
    _models(respx_mock)
    respx_mock.post(GROQ_CHAT).mock(side_effect=httpx.TimeoutException("slow"))
    result = await provider_test_routes._probe("groq", "", db_session, _row())
    assert result.verdict == "network_error"


async def test_no_key_at_all_is_not_configured(respx_mock, db_session, monkeypatch):
    monkeypatch.setattr("app.config.settings.GROQ_API_KEY", "", raising=False)
    result = await provider_test_routes._probe("groq", "", db_session, _row())
    assert result.verdict == "not_configured"
    assert result.key_valid is False


# --------------------------------------------------------------------------
# Same code path as routing
# --------------------------------------------------------------------------


async def test_probe_resolves_the_model_through_the_shared_resolver(
    respx_mock, db_session, monkeypatch
):
    """If the test button and routing disagreed, a green button would lie."""
    from app.providers import model_registry

    seen: list[str] = []
    real = model_registry.resolve_model

    async def spy(provider, api_key, override=None, force_refresh=False):
        seen.append(provider)
        return await real(provider, api_key, override=override, force_refresh=force_refresh)

    monkeypatch.setattr(provider_test_routes, "resolve_model", spy)
    _models(respx_mock)
    respx_mock.post(GROQ_CHAT).mock(return_value=httpx.Response(200, json=GROQ_OK))

    await provider_test_routes._probe("groq", "", db_session, _row())
    assert seen == ["groq"]


async def test_probe_hits_the_same_url_the_router_would(respx_mock, db_session):
    """Asserted at the wire: the URL the probe POSTs is the routing URL."""
    _models(respx_mock)
    route = respx_mock.post(GROQ_CHAT).mock(return_value=httpx.Response(200, json=GROQ_OK))

    await provider_test_routes._probe("groq", "", db_session, _row())

    assert route.called
    sent = route.calls[0].request
    assert str(sent.url) == GROQ_CHAT
    # A 1-token probe: cheap enough that clicking the button costs nothing.
    import json

    assert json.loads(sent.content)["max_tokens"] == 1


async def test_the_probe_sends_the_callers_own_key(respx_mock, db_session):
    _models(respx_mock)
    route = respx_mock.post(GROQ_CHAT).mock(return_value=httpx.Response(200, json=GROQ_OK))
    await provider_test_routes._probe("groq", "", db_session, _row())
    assert route.calls[0].request.headers["authorization"] == f"Bearer {SECRET}"


# --------------------------------------------------------------------------
# No secret anywhere
# --------------------------------------------------------------------------


async def test_no_key_in_the_response_or_the_models(respx_mock, db_session):
    _models(respx_mock)
    respx_mock.post(GROQ_CHAT).mock(
        return_value=httpx.Response(401, json={"error": f"bad key {SECRET}"})
    )
    result = await provider_test_routes._probe("groq", "", db_session, _row())
    dumped = result.model_dump_json()
    assert SECRET not in dumped
    assert "gsk_TEST" not in dumped
    # The provider's own echo of the key must be redacted too.
    assert "***" in (result.error or "")


async def test_no_key_in_a_model_not_found_error(respx_mock, db_session):
    _models(respx_mock)
    respx_mock.post(GROQ_CHAT).mock(
        return_value=httpx.Response(404, text=f'{{"error":"no model","key":"{SECRET}"}}')
    )
    result = await provider_test_routes._probe("groq", "", db_session, _row())
    assert SECRET not in result.model_dump_json()


# --------------------------------------------------------------------------
# Endpoint shape
# --------------------------------------------------------------------------


def test_routes_are_registered():
    paths = {r.path for r in provider_test_routes.router.routes}
    assert "/provider-test/{provider}" in paths
    assert "/provider-test/{provider}/models" in paths


def test_verdict_table_covers_every_outcome_a_provider_can_report():
    from app.providers.outcomes import OUTCOMES

    missing = set(OUTCOMES) - set(provider_test_routes.VERDICT_BY_OUTCOME)
    assert not missing, f"no verdict for {missing}"


def _row():
    """A stand-in for the caller's ApiKeyDB row (only .id is read)."""

    class _Row:
        id = 1

    return _Row()
