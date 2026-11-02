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
# Protected endpoints — 401 + OpenAI-SDK-compatible semantics where relevant.
# --------------------------------------------------------------------------


async def test_route_requires_key(client):
    resp = await client.post("/api/v1/route", json={"prompt": "hi"})
    assert resp.status_code == 401


async def test_v1_chat_completions_requires_key(client):
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "routellm-auto",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 401


async def test_keys_me_requires_key(client):
    assert (await client.get("/api/v1/keys/me")).status_code == 401
    assert (await client.delete("/api/v1/keys/me")).status_code == 401


async def test_provider_keys_require_key(client):
    assert (await client.get("/api/v1/provider-keys")).status_code == 401
    assert (await client.put(
        "/api/v1/provider-keys/groq", json={"api_key": "gsk_x"}
    )).status_code == 401
    assert (await client.delete("/api/v1/provider-keys/groq")).status_code == 401
