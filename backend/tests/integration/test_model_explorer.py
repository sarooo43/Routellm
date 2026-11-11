"""Model explorer + candidate walking, end to end (all upstream stubbed).

The headline scenario: a new Gemini key's list-models shows
gemini-2.5-flash, but calling it returns 404 "no longer available to new
users" while gemini-3.8-flash works. Routing AND the Test endpoints must
land on 3.8 and remember 2.5 as denied — never misreporting an auth error.
"""

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlmodel import select

from app.api.routes import limiter
from app.config import settings
from app.core import model_discovery as md
from app.core.provider_key_crypto import decrypt_provider_key
from app.main import app
from app.models import ProviderModelStatusDB
from app.providers.base import (
    ModelProvider,
    make_error,
    make_success,
)

GEMINI_SECRET = "AIzaSyDUMMY_explorer_secret_123"


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setattr(
        settings, "PROVIDER_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    # No operator keys: keep routing strictly BYOK for determinism.
    monkeypatch.setattr(settings, "GROQ_API_KEY", None)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", None)
    monkeypatch.setattr(settings, "GEMINI_MODEL", None)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def _fresh_caches():
    md.clear_caches()
    yield
    md.clear_caches()


GEMINI_MODELS = [
    md.DiscoveredModel(
        id="gemini-2.5-flash",
        display_name="Gemini 2.5 Flash",
        version=(2, 5),
        created=None,
        context_window=1_000_000,
        max_output=8192,
        chat_capable=True,
        capabilities=["generateContent"],
    ),
    md.DiscoveredModel(
        id="gemini-3.8-flash",
        display_name="Gemini 3.8 Flash",
        version=(3, 8),
        created=None,
        context_window=2_000_000,
        max_output=16384,
        chat_capable=True,
        capabilities=["generateContent"],
    ),
    md.DiscoveredModel(
        id="gemini-2.5-pro",
        display_name="Gemini 2.5 Pro",
        version=(2, 5),
        created=None,
        context_window=1_000_000,
        max_output=8192,
        chat_capable=True,
        capabilities=["generateContent"],
    ),
    md.DiscoveredModel(
        id="text-embedding-004",
        display_name="Text Embedding 004",
        version=None,
        created=None,
        context_window=None,
        max_output=None,
        chat_capable=False,
        capabilities=["embedding"],
    ),
]

NOT_FOUND_MESSAGE = (
    "models/gemini-2.5-flash is no longer available to new users. "
    "Please update your code to use models/gemini-3.8-flash"
)


class ModelAwareGeminiStub(ModelProvider):
    """Real routing behavior: 2.5 → 404 model_not_found, others → 200."""

    provider_name = "gemini"
    DEFAULT_MODEL = "gemini-2.5-flash"

    def __init__(self):
        self.calls: list[str] = []
        self.kwargs_seen: list[dict] = []

    async def generate(self, prompt: str, **kwargs):
        model = kwargs.get("model", self.DEFAULT_MODEL)
        self.calls.append(model)
        self.kwargs_seen.append(kwargs)
        if model == "gemini-2.5-flash":
            return make_error(
                "gemini", model, f"model_not_found: {NOT_FOUND_MESSAGE}", 10.0
            )
        return make_success("gemini", model, f"{model} says hi", 5, 20.0)


@pytest.fixture
async def client(monkeypatch, db_session, breaker, tracker, redis_client):
    from app.db.session import get_session

    async def override_session():
        yield db_session

    monkeypatch.setattr(app, "dependency_overrides", {get_session: override_session})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def generate_key(client, label="Explorer test"):
    response = await client.post("/api/v1/keys/generate", json={"label": label})
    body = response.json()
    return {"X-API-Key": body["api_key"]}


@pytest.fixture
def stub_gemini(monkeypatch):
    stub = ModelAwareGeminiStub()
    from app.providers.registry import PROVIDER_REGISTRY

    monkeypatch.setitem(PROVIDER_REGISTRY, "gemini", stub)
    return stub


@pytest.fixture
def fake_discovery(monkeypatch):
    async def _discover(provider, api_key, force=False):
        assert api_key == GEMINI_SECRET
        return list(GEMINI_MODELS), None

    monkeypatch.setattr(md, "discover_models", _discover)
    return _discover


# --------------------------------------------------------------------------
# Routing: candidate walking lands on gemini-3.8-flash
# --------------------------------------------------------------------------


async def test_routing_walks_from_404_model_to_working_replacement(
    client, stub_gemini, fake_discovery
):
    headers = await generate_key(client)
    save = await client.put(
        "/api/v1/provider-keys/gemini", json={"api_key": GEMINI_SECRET}, headers=headers
    )
    assert save.status_code == 200

    response = await client.post(
        "/api/v1/route", json={"prompt": "hello"}, headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["model_used"] == "gemini"

    # The stub saw BOTH candidates, in rank order, and never misreported auth.
    assert stub_gemini.calls == ["gemini-2.5-flash", "gemini-3.8-flash"]
    for kwargs in stub_gemini.kwargs_seen:
        assert kwargs["provider_api_key"] == GEMINI_SECRET

    # The failed model landed in the denied set for THIS key.
    assert md.get_cached_status(GEMINI_SECRET, "gemini", "gemini-2.5-flash") == "denied"
    assert (
        md.get_cached_status(GEMINI_SECRET, "gemini", "gemini-3.8-flash") == "works"
    )

    # The fallback chain shows the per-model walk steps.
    chain = body["fallback_chain_attempted"]
    models_in_chain = [step.get("model") for step in chain if step.get("model")]
    assert "gemini-2.5-flash" in models_in_chain
    assert "gemini-3.8-flash" in models_in_chain
    assert response.text.find(GEMINI_SECRET) == -1


async def test_denied_model_is_skipped_on_the_next_request(
    client, stub_gemini, fake_discovery
):
    headers = await generate_key(client)
    await client.put(
        "/api/v1/provider-keys/gemini", json={"api_key": GEMINI_SECRET}, headers=headers
    )
    first = await client.post("/api/v1/route", json={"prompt": "hi"}, headers=headers)
    assert first.json()["success"] is True
    assert stub_gemini.calls == ["gemini-2.5-flash", "gemini-3.8-flash"]

    # Second request: 2.5 is denied (cached) → skipped without an upstream call.
    second = await client.post("/api/v1/route", json={"prompt": "hi"}, headers=headers)
    assert second.json()["success"] is True
    assert stub_gemini.calls == ["gemini-2.5-flash", "gemini-3.8-flash", "gemini-3.8-flash"]


# --------------------------------------------------------------------------
# Test connection / per-model test endpoints (same code path)
# --------------------------------------------------------------------------


async def test_test_connection_reports_walk_steps_and_working_model(
    client, stub_gemini, fake_discovery
):
    """The ONE Test-connection route: POST /provider-test/{provider}."""
    headers = await generate_key(client)
    await client.put(
        "/api/v1/provider-keys/gemini", json={"api_key": GEMINI_SECRET}, headers=headers
    )
    response = await client.post("/api/v1/provider-test/gemini", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "valid"
    assert body["model"] == "gemini-3.8-flash"
    # Auto walks rank order: the newest plain flash is tried first and works,
    # so nothing else is attempted on this request.
    attempted = [step["model"] for step in body["attempted"]]
    assert attempted == ["gemini-3.8-flash"]
    assert GEMINI_SECRET not in response.text


async def test_per_model_test_marks_denied_and_rate_limited(
    client, stub_gemini, fake_discovery, db_session
):
    headers = await generate_key(client)
    await client.put(
        "/api/v1/provider-keys/gemini", json={"api_key": GEMINI_SECRET}, headers=headers
    )

    denied = await client.post(
        "/api/v1/provider-keys/gemini/models/gemini-2.5-flash/test", headers=headers
    )
    assert denied.status_code == 200
    assert denied.json()["status"] == "denied"
    # model_not_found is NEVER reported as an auth error.
    assert "auth" not in denied.json()["status"]

    works = await client.post(
        "/api/v1/provider-keys/gemini/models/gemini-3.8-flash/test", headers=headers
    )
    assert works.json()["status"] == "works"

    rows = {
        row.model_id: row
        for row in (
            await db_session.execute(select(ProviderModelStatusDB))
        ).scalars().all()
    }
    assert rows["gemini-2.5-flash"].status == "denied"
    assert rows["gemini-3.8-flash"].status == "works"
    assert rows["gemini-2.5-flash"].last_error and NOT_FOUND_MESSAGE[:40] in rows[
        "gemini-2.5-flash"
    ].last_error


async def test_429_marks_rate_limited_not_denied(client, monkeypatch, fake_discovery):
    class RateLimitedStub(ModelProvider):
        provider_name = "gemini"
        DEFAULT_MODEL = "gemini-3.8-flash"

        async def generate(self, prompt: str, **kwargs):
            return make_error(
                "gemini", kwargs.get("model", self.DEFAULT_MODEL),
                "rate_limit_exceeded", 5.0,
            )

    from app.providers.registry import PROVIDER_REGISTRY

    stub = RateLimitedStub()
    monkeypatch.setitem(PROVIDER_REGISTRY, "gemini", stub)
    headers = await generate_key(client)
    await client.put(
        "/api/v1/provider-keys/gemini", json={"api_key": GEMINI_SECRET}, headers=headers
    )
    response = await client.post(
        "/api/v1/provider-keys/gemini/models/gemini-3.8-flash/test", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rate_limited"
    assert (
        md.get_cached_status(GEMINI_SECRET, "gemini", "gemini-3.8-flash")
        == "rate_limited"
    )


# --------------------------------------------------------------------------
# GET models endpoint: sorting, tiers, hidden count, fail-soft
# --------------------------------------------------------------------------


async def test_models_endpoint_sorted_free_first_and_hides_non_chat(
    client, stub_gemini, fake_discovery
):
    headers = await generate_key(client)
    await client.put(
        "/api/v1/provider-keys/gemini", json={"api_key": GEMINI_SECRET}, headers=headers
    )
    response = await client.get("/api/v1/provider-keys/gemini/models", headers=headers)
    assert response.status_code == 200
    body = response.json()
    ids = [m["id"] for m in body["models"]]
    assert ids == ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-2.5-pro"]
    assert body["hidden_non_chat_count"] == 1
    tiers = {m["id"]: m["tier"] for m in body["models"]}
    assert tiers["gemini-3.8-flash"] == "free"
    assert tiers["gemini-2.5-pro"] == "paid"
    for model in body["models"]:
        assert model["tier_source"] == "estimated"
        assert model["status"] == "untested"
    assert GEMINI_SECRET not in response.text


async def test_models_endpoint_fail_soft_on_discovery_failure(
    client, monkeypatch
):
    async def broken(provider, api_key, force=False):
        return [], "discovery_failed: HTTP 503"

    monkeypatch.setattr(md, "discover_models", broken)
    headers = await generate_key(client)
    await client.put(
        "/api/v1/provider-keys/gemini", json={"api_key": GEMINI_SECRET}, headers=headers
    )
    response = await client.get("/api/v1/provider-keys/gemini/models", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["models"] == []
    assert "discovery_failed" in (body["reason"] or "")


# --------------------------------------------------------------------------
# Pin endpoint: validation, injection attempts, precedence
# --------------------------------------------------------------------------


async def test_pin_valid_model_and_pinned_precedence_in_routing(
    client, stub_gemini, fake_discovery
):
    headers = await generate_key(client)
    await client.put(
        "/api/v1/provider-keys/gemini", json={"api_key": GEMINI_SECRET}, headers=headers
    )
    pin = await client.put(
        "/api/v1/provider-keys/gemini/model",
        json={"model_id": "gemini-2.5-pro"},
        headers=headers,
    )
    assert pin.status_code == 200
    assert pin.json()["pinned_model"] == "gemini-2.5-pro"

    # Routing now uses the pin FIRST (skipping the rank order).
    route = await client.post("/api/v1/route", json={"prompt": "hi"}, headers=headers)
    assert route.json()["success"] is True
    assert stub_gemini.calls[0] == "gemini-2.5-pro"


async def test_pin_rejects_injection_attempts(client, stub_gemini, fake_discovery):
    headers = await generate_key(client)
    await client.put(
        "/api/v1/provider-keys/gemini", json={"api_key": GEMINI_SECRET}, headers=headers
    )
    for bad in ["../x", "https://evil.com/x", "models/../secret", ""]:
        response = await client.put(
            "/api/v1/provider-keys/gemini/model",
            json={"model_id": bad},
            headers=headers,
        )
        assert response.status_code == 422, bad


async def test_pin_rejects_unknown_model_id(client, stub_gemini, fake_discovery):
    headers = await generate_key(client)
    await client.put(
        "/api/v1/provider-keys/gemini", json={"api_key": GEMINI_SECRET}, headers=headers
    )
    response = await client.put(
        "/api/v1/provider-keys/gemini/model",
        json={"model_id": "gemini-9.9-not-in-list"},
        headers=headers,
    )
    assert response.status_code == 422


async def test_pin_null_returns_to_auto(client, stub_gemini, fake_discovery):
    headers = await generate_key(client)
    await client.put(
        "/api/v1/provider-keys/gemini", json={"api_key": GEMINI_SECRET}, headers=headers
    )
    await client.put(
        "/api/v1/provider-keys/gemini/model",
        json={"model_id": "gemini-2.5-pro"},
        headers=headers,
    )
    auto = await client.put(
        "/api/v1/provider-keys/gemini/model", json={"model_id": None}, headers=headers
    )
    assert auto.status_code == 200
    assert auto.json()["pinned_model"] is None


# --------------------------------------------------------------------------
# Auth + secret hygiene
# --------------------------------------------------------------------------


async def test_explorer_endpoints_require_routellm_key(client, monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_API_KEY", True)
    assert (
        await client.get("/api/v1/provider-keys/gemini/models")
    ).status_code == 401
    assert (
        await client.post("/api/v1/provider-test/gemini")
    ).status_code == 401
    assert (
        await client.put(
            "/api/v1/provider-keys/gemini/model", json={"model_id": None}
        )
    ).status_code == 401


async def test_no_provider_key_configured_fails_clean(client, fake_discovery):
    headers = await generate_key(client)
    response = await client.get("/api/v1/provider-keys/gemini/models", headers=headers)
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]
