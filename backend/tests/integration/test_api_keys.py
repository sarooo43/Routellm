"""Lightweight API-key auth: hashed storage, protected core routes, usage."""

import hashlib

import httpx
import pytest
from sqlalchemy import select

from app.core import executor as executor_module
from app.core.chaos import ChaosController
from app.main import app
from app.models import ApiKeyDB
from tests.integration.conftest import StubProvider, fail, ok


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
        groq=StubProvider("groq", [ok("groq", text="Hello", tokens=21)]),
        gemini=StubProvider("gemini", [ok("gemini")]),
        deepseek=StubProvider("deepseek", [ok("deepseek")]),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def generate(client, label="Demo key"):
    response = await client.post("/api/v1/keys/generate", json={"label": label})
    assert response.status_code == 201
    return response.json()


async def test_generate_returns_raw_key_once_and_db_stores_only_hash(client, db_session):
    body = await generate(client, "Portfolio demo")
    raw = body["api_key"]
    assert raw.startswith("rk_")
    assert len(raw) == 35
    assert body["key_prefix"] == raw[:8]
    assert body["label"] == "Portfolio demo"
    assert body["warning"] == "Save this key now — it will not be shown again."

    row = (await db_session.execute(select(ApiKeyDB))).scalars().one()
    assert row.key_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert row.key_hash != raw
    assert row.key_prefix == raw[:8]
    assert raw not in (row.key_hash, row.key_prefix, row.label)
    # The full secret is not present in any persisted column on the row.
    assert all(raw not in str(value) for value in row.__dict__.values())


async def test_core_route_requires_key(client):
    missing = await client.post("/api/v1/route", json={"prompt": "hi"})
    assert missing.status_code == 401
    assert missing.json()["detail"] == "Invalid or missing API key"

    invalid = await client.post(
        "/api/v1/route", json={"prompt": "hi"}, headers={"X-API-Key": "rk_fake"}
    )
    assert invalid.status_code == 401


async def test_valid_key_routes_and_increments_usage(client, db_session):
    key = await generate(client)
    headers = {"X-API-Key": key["api_key"]}
    response = await client.post("/api/v1/route", json={"prompt": "hi"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["success"] is True

    row = (await db_session.execute(select(ApiKeyDB))).scalars().one()
    assert row.total_requests == 1
    assert row.total_tokens_used == 21
    assert row.last_used_at is not None

    me = await client.get("/api/v1/keys/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["total_requests"] == 1
    assert me.json()["total_tokens_used"] == 21
    assert me.json()["key_prefix"] == key["key_prefix"]


async def test_revoked_key_is_rejected(client, db_session):
    key = await generate(client)
    headers = {"X-API-Key": key["api_key"]}
    revoked = await client.delete("/api/v1/keys/me", headers=headers)
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"

    row = (await db_session.execute(select(ApiKeyDB))).scalars().one()
    assert row.is_active is False
    response = await client.post("/api/v1/route", json={"prompt": "hi"}, headers=headers)
    assert response.status_code == 401


async def test_keys_only_see_their_own_usage(client):
    first = await generate(client, "First")
    second = await generate(client, "Second")
    await client.post(
        "/api/v1/route", json={"prompt": "hi"}, headers={"X-API-Key": first["api_key"]}
    )
    me = await client.get("/api/v1/keys/me", headers={"X-API-Key": second["api_key"]})
    assert me.status_code == 200
    assert me.json()["label"] == "Second"
    assert me.json()["total_requests"] == 0


async def test_openai_compat_requires_key_and_tracks_usage(client, db_session):
    missing = await client.post(
        "/v1/chat/completions",
        json={"model": "routellm-auto", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "invalid_api_key"

    key = await generate(client)
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "routellm-auto", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-API-Key": key["api_key"]},
    )
    assert response.status_code == 200
    row = (await db_session.execute(select(ApiKeyDB))).scalars().one()
    assert row.total_requests == 1
    assert row.total_tokens_used == 21


async def test_failed_generation_does_not_increment_success_counters(
    client, db_session, install_stubs
):
    install_stubs(
        groq=StubProvider("groq", [fail("groq")]),
        gemini=StubProvider("gemini", [fail("gemini")]),
        deepseek=StubProvider("deepseek", [fail("deepseek")]),
    )
    key = await generate(client)
    response = await client.post(
        "/api/v1/route", json={"prompt": "hi"}, headers={"X-API-Key": key["api_key"]}
    )
    assert response.status_code == 200
    assert response.json()["success"] is False
    row = (await db_session.execute(select(ApiKeyDB))).scalars().one()
    assert row.total_requests == 0
    assert row.total_tokens_used == 0


async def test_health_discovery_and_catalog_remain_public(client):
    assert (await client.get("/api/v1/health")).status_code in (200, 503)
    assert (await client.get("/v1/models")).status_code == 200
    assert (await client.get("/api/v1/catalog")).status_code == 200
