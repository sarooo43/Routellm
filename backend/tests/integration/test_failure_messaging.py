"""What a failed route looks like to the person who clicked Send.

Everything here is a response *body* assertion, because the field report was
not about a wrong status code — the API was returning 200 the whole time. The
complaint was that the body said "unexpected_error: ConnectError" and the user
concluded the gateway was down.

Rule under test: a failed route must (a) keep returning its documented status,
(b) carry a diagnosis that names the failing provider, and (c) never present the
raw provider code as the whole story.
"""

import httpx
import pytest

from app.core.error_messages import CONSOLE_URLS


@pytest.fixture
async def client(monkeypatch, db_session, breaker, tracker, redis_client, auth_headers):
    """The real ASGI app with a test session, as the other route tests do."""
    from app.api import routes as routes_module
    from app.core import executor as executor_module
    from app.core.chaos import ChaosController
    from app.db.session import get_optional_session, get_session
    from app.main import app

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_optional_session] = _override_session
    monkeypatch.setattr(routes_module, "_get_breaker", lambda: breaker)
    monkeypatch.setattr(routes_module, "_get_quota", lambda: tracker)
    monkeypatch.setattr(
        executor_module,
        "_ensure_shared_clients",
        lambda: (breaker, tracker, ChaosController(redis_client)),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", headers=auth_headers
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
def failing_provider(monkeypatch):
    """Force every provider to fail the way an offline machine fails."""
    from app.core import executor

    class _Unreachable:
        provider_name = "groq"

        async def generate(self, *args, **kwargs):
            from app.providers.base import make_error

            return make_error("groq", "llama-3.1-8b-instant", "ConnectError", 12.0)

    registry = dict(executor.PROVIDER_REGISTRY)
    registry["groq"] = _Unreachable()
    monkeypatch.setattr(executor, "PROVIDER_REGISTRY", registry)
    return registry


async def test_route_reports_a_diagnosis_not_a_class_name(client, failing_provider):
    response = await client.post(
        "/api/v1/route", json={"prompt": "hello", "force_provider": "groq"}
    )
    # A failed route is still a completed request: the documented contract is
    # 200 with success=false, not a 5xx. Changing that would break clients.
    assert response.status_code == 200
    body = response.json()

    assert body["success"] is False
    error = body["error"]
    assert error.startswith("network_unreachable:")
    assert "groq" in error
    assert "gateway is working" in error
    # The bare code is kept, but only as evidence inside the sentence.
    assert error != "unexpected_error: ConnectError"
    assert "ConnectError" in error


async def test_the_attempt_chain_is_explained_too(client, failing_provider):
    """The Explorer renders per-attempt errors; those must read as prose."""
    body = (
        await client.post(
            "/api/v1/route", json={"prompt": "hello", "force_provider": "groq"}
        )
    ).json()
    attempt = body["fallback_chain_attempted"][0]
    assert attempt["provider_name"] == "groq"
    assert attempt["success"] is False
    assert attempt["error"].startswith("network_unreachable:")


async def test_the_stored_log_keeps_the_raw_code(client, failing_provider):
    """Diagnosis is for humans; the log is for debugging, so it stays raw."""
    request_id = (
        await client.post(
            "/api/v1/route", json={"prompt": "hello", "force_provider": "groq"}
        )
    ).json()["request_id"]

    logs = (await client.get("/api/v1/logs", params={"limit": 50})).json()
    rows = [row for row in logs["logs"] if row["request_id"] == request_id]
    assert rows, "the failed attempt must still be recorded"
    assert "ConnectError" in (rows[0].get("error_message") or "")


async def test_openai_compat_failure_carries_the_same_diagnosis(client, failing_provider):
    """SDK users get the message too, in OpenAI's envelope."""
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "groq", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 502
    message = response.json()["error"]["message"]
    assert message.startswith("network_unreachable:")
    assert "groq" in message
    assert response.json()["error"]["code"] == "all_providers_failed"


async def test_auth_failure_tells_the_user_where_to_fix_the_key(client, monkeypatch):
    from app.core import executor
    from app.providers.base import make_error

    class _Rejected:
        provider_name = "groq"

        async def generate(self, *args, **kwargs):
            return make_error("groq", "llama-3.1-8b-instant", "auth_failed", 8.0)

    registry = dict(executor.PROVIDER_REGISTRY)
    registry["groq"] = _Rejected()
    monkeypatch.setattr(executor, "PROVIDER_REGISTRY", registry)

    body = (
        await client.post(
            "/api/v1/route", json={"prompt": "hello", "force_provider": "groq"}
        )
    ).json()
    assert body["error"].startswith("auth_failed:")
    assert CONSOLE_URLS["groq"] in body["error"]
    assert "Providers page" in body["error"]


async def test_a_successful_route_has_no_error_sentence(client):
    """The demo provider is untouched: success must not grow a diagnosis."""
    body = (
        await client.post(
            "/api/v1/route", json={"prompt": "hello", "force_provider": "mock"}
        )
    ).json()
    assert body["success"] is True
    assert body["error"] is None
    assert body["fallback_chain_attempted"][0]["error"] is None
