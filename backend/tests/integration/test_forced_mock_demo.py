"""Forced `mock` must never answer `missing_provider_key` (T5).

OBSERVED LIVE (owner's Codespace, main at 136fae0, real Gemini key stored):
`POST /api/v1/route {"force_provider": "mock"}` answered
`forced_provider_failed_no_fallback error=missing_provider_key`.

Root cause (pre-fix): the BYOK-isolation gate computed
``missing_byok = bool(provider_api_keys) and "mock" not in provider_api_keys``
— a caller with ANY stored provider key was denied the demo provider
structurally. A caller with ZERO stored keys got the mock (which is why the
earlier sandbox smoke passed: its SQLite caller had no BYOK rows at all).
"""

import httpx
import pytest
from cryptography.fernet import Fernet

from app.api.routes import limiter
from app.config import settings
from app.core import executor as executor_module
from app.core.chaos import ChaosController
from app.main import app
from tests.integration.conftest import StubProvider, ok


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setattr(
        settings, "PROVIDER_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8")
    )


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    yield
    limiter.reset()


@pytest.fixture
async def client(monkeypatch, db_session, breaker, tracker, redis_client, install_stubs):
    from app.db.session import get_session

    async def override_session():
        yield db_session

    monkeypatch.setattr(app, "dependency_overrides", {get_session: override_session})
    monkeypatch.setattr(
        executor_module,
        "_ensure_shared_clients",
        lambda: (breaker, tracker, ChaosController(redis_client)),
    )
    install_stubs(
        gemini=StubProvider("gemini", [ok("gemini", text="gemini ok", tokens=13)] * 10),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _generate_and_store_gemini_key(client):
    response = await client.post("/api/v1/keys/generate", json={"label": "mock demo"})
    body = response.json()
    headers = {"X-API-Key": body["api_key"]}
    save = await client.put(
        "/api/v1/provider-keys/gemini", json={"api_key": "AIzaFAKEKEY000000"}, headers=headers
    )
    assert save.status_code == 200, save.text
    return headers


async def test_forced_mock_succeeds_even_with_a_stored_byok_key(client):
    """THE OBSERVED FAILURE, as a regression test.

    A BYOK caller (Gemini key stored) forces mock: the demo provider is
    synthetic and touches no credential, so BYOK isolation must not deny it.
    """
    headers = await _generate_and_store_gemini_key(client)
    response = await client.post(
        "/api/v1/route", json={"prompt": "hi", "force_provider": "mock"}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["model_used"] == "mock"
    assert body["result"].startswith("[DEMO")


async def test_forced_mock_succeeds_with_no_keys_at_all(client):
    """No BYOK rows and no env keys: the fresh-clone demo path still works."""
    headers_response = await client.post("/api/v1/keys/generate", json={"label": "anon"})
    headers = {"X-API-Key": headers_response.json()["api_key"]}
    response = await client.post(
        "/api/v1/route", json={"prompt": "hi", "force_provider": "mock"}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["model_used"] == "mock"


async def test_demo_disabled_gives_a_clear_message_never_missing_provider_key(
    client, monkeypatch
):
    """Demo mode off = a deliberate setting, worded as one — never a key error."""
    monkeypatch.setattr(settings, "DEMO_MODE_ENABLED", False)
    from app.providers.registry import PROVIDER_REGISTRY

    removed = PROVIDER_REGISTRY.pop("mock", None)
    try:
        headers_response = await client.post("/api/v1/keys/generate", json={"label": "x"})
        headers = {"X-API-Key": headers_response.json()["api_key"]}
        response = await client.post(
            "/api/v1/route",
            json={"prompt": "hi", "force_provider": "mock"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["success"] is False
        assert "Demo mode is off" in body["error"]
        assert "missing_provider_key" not in body["error"]
    finally:
        if removed is not None:
            PROVIDER_REGISTRY["mock"] = removed


async def test_forced_real_provider_without_byok_still_reports_missing_key(client):
    """BYOK isolation for REAL providers is unchanged and still structural."""
    headers = await _generate_and_store_gemini_key(client)
    response = await client.post(
        "/api/v1/route", json={"prompt": "hi", "force_provider": "groq"}, headers=headers
    )
    body = response.json()
    assert body["success"] is False
    assert "missing_provider_key" in body["error"]
