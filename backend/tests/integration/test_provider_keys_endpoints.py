"""Provider-keys endpoint tests added by the audit follow-up (T5).

Covers HTTP behaviours the earlier suites did not exercise end to end:
the list endpoint's happy path, the DELETE cascade of the per-(key,
provider) pin and walk-status rows, and diagnostics on an unknown
provider. All through httpx.ASGITransport against the real app;
upstreams are stubbed, nothing spends provider credits.
"""

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.api.routes import limiter
from app.config import settings
from app.main import app
from app.models import ProviderModelPinDB, ProviderModelStatusDB, ProviderKeyDB


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setattr(settings, "PROVIDER_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    yield
    limiter.reset()


@pytest.fixture
async def client(monkeypatch, db_session, breaker, tracker, redis_client):
    from app.db.session import get_session

    async def override_session():
        yield db_session

    monkeypatch.setattr(app, "dependency_overrides", {get_session: override_session})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def generate_key(client, label="T5 endpoint test"):
    response = await client.post("/api/v1/keys/generate", json={"label": label})
    assert response.status_code == 201
    body = response.json()
    return body["api_key"], {"X-API-Key": body["api_key"]}


async def test_list_shows_configured_and_unconfigured_providers_without_secrets(client):
    _, headers = await generate_key(client)
    saved = await client.put(
        "/api/v1/provider-keys/groq", json={"api_key": "gsk_t5_list_test_123456"}, headers=headers
    )
    assert saved.status_code == 200

    listed = await client.get("/api/v1/provider-keys", headers=headers)
    assert listed.status_code == 200
    rows = listed.json()
    by_name = {row["provider_name"]: row for row in rows}
    # Every registered BYOK provider answers, configured or not.
    assert set(by_name) == {"gemini", "groq", "deepseek"}
    assert by_name["groq"]["configured"] is True
    assert by_name["gemini"]["configured"] is False
    # A hint is fine; the secret itself must never appear anywhere.
    text = listed.text
    assert "gsk_t5_list_test_123456" not in text
    assert "****" in by_name["groq"].get("key_hint", "")


async def test_delete_cascades_the_pin_and_walk_status_rows(client, db_session):
    _, headers = await generate_key(client)
    assert (
        await client.put(
            "/api/v1/provider-keys/gemini",
            json={"api_key": "AIzaSyT5_cascade_test_654321"},
            headers=headers,
        )
    ).status_code == 200

    # Seed the rows the cascade must remove, as routing would have.
    db_session.add(
        ProviderModelPinDB(api_key_id=1, provider_name="gemini", model_id="gemini-3.8-flash")
    )
    db_session.add(
        ProviderModelStatusDB(
            api_key_id=1, provider_name="gemini", model_id="gemini-2.5-flash", status="denied"
        )
    )
    await db_session.commit()

    keys_before = (await db_session.execute(select(ProviderKeyDB))).scalars().all()
    pins_before = (await db_session.execute(select(ProviderModelPinDB))).scalars().all()
    statuses_before = (await db_session.execute(select(ProviderModelStatusDB))).scalars().all()
    assert len(keys_before) == 1 and len(pins_before) == 1 and len(statuses_before) == 1

    deleted = await client.delete("/api/v1/provider-keys/gemini", headers=headers)
    assert deleted.status_code == 200

    assert (await db_session.execute(select(ProviderKeyDB))).scalars().all() == []
    assert (
        await db_session.execute(select(ProviderModelPinDB))
    ).scalars().all() == []
    assert (
        await db_session.execute(select(ProviderModelStatusDB))
    ).scalars().all() == []


async def test_diagnostics_on_an_unknown_provider_answers_404(client):
    _, headers = await generate_key(client)
    response = await client.post("/api/v1/provider-keys/not-a-provider/diagnostics", headers=headers)
    assert response.status_code == 404
