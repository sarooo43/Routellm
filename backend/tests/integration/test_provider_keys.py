"""Per-caller BYOK provider keys: encrypted storage, tenant isolation, routing.

All upstream calls are stubbed; no test spends real provider credits.
"""

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.api.routes import limiter
from app.config import settings
from app.core import executor as executor_module
from app.core.chaos import ChaosController
from app.core.provider_key_crypto import decrypt_provider_key
from app.main import app
from app.models import ProviderKeyDB
from tests.integration.conftest import StubProvider, fail, ok


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setattr(settings, "PROVIDER_KEY_ENCRYPTION_KEY", key)
    return key


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
        groq=StubProvider("groq", [ok("groq", text="groq ok", tokens=21)] * 10),
        gemini=StubProvider("gemini", [ok("gemini", text="gemini ok", tokens=13)] * 10),
        deepseek=StubProvider("deepseek", [ok("deepseek", text="deepseek ok", tokens=8)] * 10),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def generate_key(client, label="BYOK test"):
    response = await client.post("/api/v1/keys/generate", json={"label": label})
    assert response.status_code == 201
    body = response.json()
    return body["api_key"], {"X-API-Key": body["api_key"]}


async def test_provider_key_endpoints_require_routellm_key(client):
    assert (await client.get("/api/v1/provider-keys")).status_code == 401
    assert (
        await client.put("/api/v1/provider-keys/groq", json={"api_key": "gsk_test_123456"})
    ).status_code == 401
    assert (await client.delete("/api/v1/provider-keys/groq")).status_code == 401


async def test_put_encrypts_at_rest_and_get_returns_only_hint(client, db_session):
    raw, headers = await generate_key(client)
    provider_secret = "gsk_test_1234567890abcdef"
    response = await client.put(
        "/api/v1/provider-keys/groq", json={"api_key": provider_secret}, headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["provider_name"] == "groq"
    assert body["configured"] is True
    assert body["key_hint"] == "****cdef"
    assert provider_secret not in response.text
    assert raw not in response.text

    row = (await db_session.execute(select(ProviderKeyDB))).scalars().one()
    assert row.encrypted_key != provider_secret
    assert provider_secret not in row.encrypted_key
    assert decrypt_provider_key(row.encrypted_key) == provider_secret
    assert all(provider_secret not in str(value) for value in row.__dict__.values())

    listed = await client.get("/api/v1/provider-keys", headers=headers)
    assert listed.status_code == 200
    statuses = {item["provider_name"]: item for item in listed.json()}
    assert set(statuses) == {"groq", "gemini", "deepseek"}
    assert statuses["groq"]["configured"] is True
    assert statuses["groq"]["key_hint"] == "****cdef"
    assert statuses["gemini"]["configured"] is False
    assert provider_secret not in listed.text


async def test_upsert_updates_existing_row_and_delete_removes_it(client, db_session):
    _, headers = await generate_key(client)
    first = await client.put(
        "/api/v1/provider-keys/gemini", json={"api_key": "AIzaSyDUMMY_first_123456"}, headers=headers
    )
    assert first.status_code == 200
    second = await client.put(
        "/api/v1/provider-keys/gemini", json={"api_key": "AIzaSyDUMMY_second_654321"}, headers=headers
    )
    assert second.status_code == 200
    rows = (await db_session.execute(select(ProviderKeyDB))).scalars().all()
    assert len(rows) == 1
    assert rows[0].key_hint == "****4321"

    deleted = await client.delete("/api/v1/provider-keys/gemini", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json() == {"status": "deleted", "provider_name": "gemini"}
    assert (await db_session.execute(select(ProviderKeyDB))).scalars().all() == []
    assert (await client.delete("/api/v1/provider-keys/gemini", headers=headers)).status_code == 404


async def test_provider_keys_are_isolated_per_routellm_key(client, db_session):
    _, headers_a = await generate_key(client, "Tenant A")
    _, headers_b = await generate_key(client, "Tenant B")
    secret_a = "gsk_test_tenant_a_123456"
    secret_b = "gsk_test_tenant_b_654321"
    await client.put("/api/v1/provider-keys/groq", json={"api_key": secret_a}, headers=headers_a)
    await client.put("/api/v1/provider-keys/groq", json={"api_key": secret_b}, headers=headers_b)

    rows = (await db_session.execute(select(ProviderKeyDB))).scalars().all()
    assert len(rows) == 2
    assert {decrypt_provider_key(row.encrypted_key) for row in rows} == {secret_a, secret_b}

    listed_b = await client.get("/api/v1/provider-keys", headers=headers_b)
    assert listed_b.status_code == 200
    assert secret_a not in listed_b.text
    statuses_b = {item["provider_name"]: item for item in listed_b.json()}
    assert statuses_b["groq"]["key_hint"] == "****4321"

    deleted_b = await client.delete("/api/v1/provider-keys/groq", headers=headers_b)
    assert deleted_b.status_code == 200
    remaining = (await db_session.execute(select(ProviderKeyDB))).scalars().all()
    assert len(remaining) == 1
    assert decrypt_provider_key(remaining[0].encrypted_key) == secret_a


async def test_route_uses_callers_provider_key_and_restricts_fallback(client, install_stubs):
    stubs = install_stubs(
        groq=StubProvider("groq", [ok("groq")]),
        gemini=StubProvider("gemini", [ok("gemini")]),
        deepseek=StubProvider("deepseek", [ok("deepseek")]),
    )
    _, headers = await generate_key(client)
    secret = "gsk_test_routing_123456"
    await client.put("/api/v1/provider-keys/groq", json={"api_key": secret}, headers=headers)

    response = await client.post(
        "/api/v1/route",
        json={"prompt": "hello", "force_provider": "groq"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert stubs["groq"].kwargs_seen[-1]["provider_api_key"] == secret
    assert stubs["gemini"].calls == 0
    assert stubs["deepseek"].calls == 0


async def test_auto_routing_only_attempts_configured_byok_providers(client, install_stubs):
    stubs = install_stubs(
        groq=StubProvider("groq", [fail("groq")]),
        gemini=StubProvider("gemini", [ok("gemini")]),
        deepseek=StubProvider("deepseek", [ok("deepseek")]),
    )
    _, headers = await generate_key(client)
    await client.put(
        "/api/v1/provider-keys/gemini", json={"api_key": "AIzaSyDUMMY_byok_123456"}, headers=headers
    )
    response = await client.post("/api/v1/route", json={"prompt": "hello"}, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["model_used"] == "gemini"
    assert stubs["groq"].calls == 0
    assert stubs["deepseek"].calls == 0
    assert stubs["gemini"].kwargs_seen[-1]["provider_api_key"] == "AIzaSyDUMMY_byok_123456"


async def test_forced_unconfigured_byok_provider_fails_without_upstream_call(client, install_stubs):
    stubs = install_stubs(
        groq=StubProvider("groq", [ok("groq")]),
        gemini=StubProvider("gemini", [ok("gemini")]),
    )
    _, headers = await generate_key(client)
    await client.put("/api/v1/provider-keys/groq", json={"api_key": "gsk_test_forced_123456"}, headers=headers)
    response = await client.post(
        "/api/v1/route",
        json={"prompt": "hello", "force_provider": "gemini"},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["error"] == "missing_provider_key"
    assert stubs["gemini"].calls == 0
    assert stubs["groq"].calls == 0


async def test_openai_compat_uses_callers_provider_key(client, install_stubs):
    stubs = install_stubs(deepseek=StubProvider("deepseek", [ok("deepseek", tokens=8)]))
    _, headers = await generate_key(client)
    secret = "sk-test-byok-deepseek-123456"
    await client.put("/api/v1/provider-keys/deepseek", json={"api_key": secret}, headers=headers)
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "deepseek", "messages": [{"role": "user", "content": "hi"}]},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["model"] == "deepseek"
    assert stubs["deepseek"].kwargs_seen[-1]["provider_api_key"] == secret


async def test_legacy_caller_without_byok_keeps_operator_kwargs(client, install_stubs):
    stubs = install_stubs(groq=StubProvider("groq", [ok("groq")]))
    _, headers = await generate_key(client)
    response = await client.post(
        "/api/v1/route", json={"prompt": "hello", "force_provider": "groq"}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert "provider_api_key" not in stubs["groq"].kwargs_seen[-1]


async def test_missing_encryption_key_fails_closed_for_writes_and_byok_routing(
    client, monkeypatch, db_session
):
    _, headers = await generate_key(client)
    await client.put("/api/v1/provider-keys/groq", json={"api_key": "gsk_test_failclosed_123"}, headers=headers)
    monkeypatch.setattr(settings, "PROVIDER_KEY_ENCRYPTION_KEY", None)

    write = await client.put(
        "/api/v1/provider-keys/gemini", json={"api_key": "AIzaSyDUMMY_failclosed"}, headers=headers
    )
    assert write.status_code == 503
    route = await client.post("/api/v1/route", json={"prompt": "hello"}, headers=headers)
    assert route.status_code == 503
    chat = await client.post(
        "/v1/chat/completions",
        json={"model": "routellm-auto", "messages": [{"role": "user", "content": "hi"}]},
        headers=headers,
    )
    assert chat.status_code == 503
    assert chat.json()["error"]["type"] == "server_error"
    row = (await db_session.execute(select(ProviderKeyDB))).scalars().one()
    assert row.encrypted_key != "gsk_test_failclosed_123"


async def test_unknown_provider_and_short_key_rejected(client):
    _, headers = await generate_key(client)
    unknown = await client.put(
        "/api/v1/provider-keys/openai", json={"api_key": "sk-test-123456"}, headers=headers
    )
    assert unknown.status_code == 404
    short = await client.put(
        "/api/v1/provider-keys/groq", json={"api_key": "short"}, headers=headers
    )
    assert short.status_code == 422
