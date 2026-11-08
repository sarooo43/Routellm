"""Auth-surface matrix: which endpoints are public vs require a RouteLLM key.

Pins the contract so a future change can't accidentally expose (or lock up)
endpoints. Every request here is made WITHOUT an X-API-Key header.

Public  (2xx without a key):  /health, /api/v1/catalog, /api/v1/models,
                              /api/v1/analytics, /api/v1/logs,
                              /api/v1/keys/generate, /v1/models, /v1/models oracle
Protected (401 without a key): POST /api/v1/route, POST /v1/chat/completions,
                              GET/DELETE /api/v1/keys/me,
                              GET/PUT/DELETE /api/v1/provider-keys*
"""

import httpx
import pytest

from app.config import settings
from app.core.api_key_manager import LOCAL_IDENTITY_LABEL, LOCAL_IDENTITY_PREFIX
from app.main import app


@pytest.fixture
async def client(db_session, breaker, tracker):
    from app.db.session import get_session

    async def override_session():
        yield db_session

    app.dependency_overrides[get_session] = override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# Public endpoints — must work with NO API key.
# --------------------------------------------------------------------------


async def test_health_is_public(client):
    assert (await client.get("/health")).status_code == 200


async def test_catalog_is_public(client):
    resp = await client.get("/api/v1/catalog")
    assert resp.status_code == 200
    assert "models" in resp.json()


async def test_models_is_public(client):
    assert (await client.get("/api/v1/models")).status_code == 200


async def test_analytics_is_public(client):
    assert (await client.get("/api/v1/analytics")).status_code == 200


async def test_logs_is_public(client):
    resp = await client.get("/api/v1/logs")
    assert resp.status_code == 200
    assert "logs" in resp.json()


async def test_keys_generate_is_public_but_201(client):
    resp = await client.post("/api/v1/keys/generate", json={"label": "matrix"})
    assert resp.status_code == 201
    assert resp.json()["api_key"].startswith("rk_")


async def test_v1_models_is_public(client):
    resp = await client.get("/v1/models")
    assert resp.status_code == 200
    assert resp.json()["object"] == "list"


# --------------------------------------------------------------------------
# REQUIRE_API_KEY=true — the cloud/team behaviour, unchanged.
# --------------------------------------------------------------------------


@pytest.fixture
async def locked_client(client, monkeypatch):
    """The same client with API keys enforced (REQUIRE_API_KEY=true)."""
    monkeypatch.setattr(settings, "REQUIRE_API_KEY", True)
    return client


async def test_route_requires_key(locked_client):
    resp = await locked_client.post("/api/v1/route", json={"prompt": "hi"})
    assert resp.status_code == 401


async def test_v1_chat_completions_requires_key(locked_client):
    resp = await locked_client.post(
        "/v1/chat/completions",
        json={
            "model": "routellm-auto",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 401


async def test_keys_me_requires_key(locked_client):
    assert (await locked_client.get("/api/v1/keys/me")).status_code == 401
    assert (await locked_client.delete("/api/v1/keys/me")).status_code == 401


async def test_provider_keys_require_key(locked_client):
    assert (await locked_client.get("/api/v1/provider-keys")).status_code == 401
    assert (await locked_client.put(
        "/api/v1/provider-keys/groq", json={"api_key": "gsk_x"}
    )).status_code == 401
    assert (await locked_client.delete("/api/v1/provider-keys/groq")).status_code == 401


async def test_invalid_key_is_rejected_in_locked_mode(locked_client):
    """A wrong key is 401 — it is never silently downgraded to anonymous."""
    resp = await locked_client.post(
        "/api/v1/route",
        json={"prompt": "hi"},
        headers={"X-API-Key": "rk_not_a_real_key"},
    )
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "ApiKey"


# --------------------------------------------------------------------------
# REQUIRE_API_KEY=false — the local-first default.
# --------------------------------------------------------------------------


async def test_local_mode_defaults_to_off():
    """No key required on a fresh clone: that is the whole point of local-first."""
    assert settings.REQUIRE_API_KEY is False


async def test_keys_me_works_without_a_key_in_local_mode(client):
    """Anonymous usage is attributed to a real, stable local identity row."""
    me = await client.get("/api/v1/keys/me")
    assert me.status_code == 200
    body = me.json()
    assert body["key_prefix"] == LOCAL_IDENTITY_PREFIX
    assert body["label"] == LOCAL_IDENTITY_LABEL
    # Same identity on every request — not a new row per call.
    again = await client.get("/api/v1/keys/me")
    assert again.json()["key_prefix"] == body["key_prefix"]
    assert again.json()["created_at"] == body["created_at"]


async def test_local_identity_is_created_once(client, db_session):
    """BYOK is stored per identity, so the row must not be duplicated."""
    from sqlalchemy import func, select

    from app.models import ApiKeyDB

    for _ in range(3):
        assert (await client.get("/api/v1/keys/me")).status_code == 200
    count = (
        await db_session.execute(
            select(func.count()).select_from(ApiKeyDB).where(
                ApiKeyDB.key_hash == "sha256:local-identity"
            )
        )
    ).scalar_one()
    assert count == 1


async def test_local_mode_still_validates_a_presented_key(client):
    """Adding a real key later must keep working without any other change."""
    created = await client.post("/api/v1/keys/generate", json={"label": "mine"})
    raw_key = created.json()["api_key"]
    me = await client.get("/api/v1/keys/me", headers={"X-API-Key": raw_key})
    assert me.status_code == 200
    assert me.json()["label"] == "mine"


async def test_local_mode_ignores_an_unknown_key(client):
    """A stale key in a script is not fatal locally — it just falls back."""
    me = await client.get(
        "/api/v1/keys/me", headers={"X-API-Key": "rk_stale_key_from_another_run"}
    )
    assert me.status_code == 200
    assert me.json()["key_prefix"] == LOCAL_IDENTITY_PREFIX


async def test_bearer_header_is_accepted_for_openai_sdk_clients(client):
    """The official OpenAI SDK sends `Authorization: Bearer <key>`."""
    created = await client.post("/api/v1/keys/generate", json={"label": "sdk"})
    raw_key = created.json()["api_key"]
    me = await client.get("/api/v1/keys/me", headers={"Authorization": f"Bearer {raw_key}"})
    assert me.status_code == 200
    assert me.json()["label"] == "sdk"
