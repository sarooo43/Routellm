"""Demo mode: a MOCK provider for a zero-key, zero-spend first run.

The value of demo mode is entirely in its honesty, so that is what is pinned:

* automatic routing NEVER picks it — a synthetic answer only happens when the
  caller named it (``force_provider: "mock"`` / OpenAI model ``mock``),
* the response says DEMO in the text and reports a ``mock`` provider name,
* it makes no network call and cannot fail unless configured to (the failure
  rate exists to exercise the fallback path deliberately),
* it is excluded from the catalog, the model-health list, BYOK status, the
  analytics legend and provider counts — i.e. from every real measurement.
"""

import httpx
import pytest

from app.config import settings
from app.core import executor as executor_module
from app.core.chaos import ChaosController
from app.providers.demo_provider import (
    DEMO_MARKER,
    DEMO_MODEL_NAME,
    DEMO_PROVIDER_NAME,
    DemoProvider,
    is_demo_provider,
)
from app.providers.registry import (
    PROVIDER_REGISTRY,
    demo_provider_available,
    demo_provider_usable,
    routable_providers,
)
from app.main import app
from tests.integration.conftest import StubProvider, expected_ranking, ok


# --------------------------------------------------------------------------
# The provider itself
# --------------------------------------------------------------------------


async def test_response_is_marked_and_uses_no_network():
    provider = DemoProvider(latency_ms=0)
    response = await provider.generate("explain how routers fail over")
    assert response.success is True
    assert response.provider_name == DEMO_PROVIDER_NAME
    assert response.model_name == DEMO_MODEL_NAME
    assert response.text.startswith(DEMO_MARKER)
    assert response.tokens_used > 0
    assert provider.calls == 1


async def test_canned_answer_tracks_the_prompt_type():
    provider = DemoProvider(latency_ms=0)
    code = await provider.generate("write python code to sort a list")
    other = await provider.generate("hello there")
    assert code.text != other.text
    assert DEMO_MARKER in code.text and DEMO_MARKER in other.text


async def test_simulated_failure_never_raises():
    provider = DemoProvider(latency_ms=0, failure_rate=1.0, seed=1)
    response = await provider.generate("hi")
    assert response.success is False
    assert response.error == "demo_simulated_failure"


async def test_latency_is_simulated_not_real():
    """A configured delay must actually be waited out (timer, not magic)."""
    import time

    provider = DemoProvider(latency_ms=120, failure_rate=0.0)
    started = time.perf_counter()
    response = await provider.generate("hi")
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert elapsed_ms >= 90  # jitter is ±20%
    assert response.latency_ms >= 90


def test_helper_identifies_the_demo_provider():
    assert is_demo_provider(DEMO_PROVIDER_NAME) is True
    assert is_demo_provider("groq") is False
    assert is_demo_provider(None) is False


# --------------------------------------------------------------------------
# Never part of automatic routing
# --------------------------------------------------------------------------


def test_demo_provider_is_registered_but_not_routable():
    assert demo_provider_available() is True
    assert DEMO_PROVIDER_NAME in PROVIDER_REGISTRY
    assert DEMO_PROVIDER_NAME not in routable_providers()


def test_demo_is_usable_only_while_no_real_key_exists(monkeypatch):
    for name in ("GROQ_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert demo_provider_usable() is True

    monkeypatch.setenv("GROQ_API_KEY", "gsk_placeholder_for_detection_only")
    assert demo_provider_usable() is False


def test_surfaces_report_real_vendors_only(monkeypatch):
    """Catalog, model health and BYOK all come from the same real set."""
    from app.core.provider_key_manager import SUPPORTED_PROVIDERS

    assert DEMO_PROVIDER_NAME not in SUPPORTED_PROVIDERS
    assert DEMO_PROVIDER_NAME not in routable_providers()


async def test_automatic_routing_never_selects_the_demo_provider(
    install_stubs, breaker, tracker, db_session, redis_client, monkeypatch
):
    """Even with every real provider failing, routing stays on real providers."""
    from app.core.executor import execute_with_fallback

    order = expected_ranking("code")
    top = order[0]
    install_stubs(
        **{
            name: StubProvider(name, [ok(name)] if name == top else [])
            for name in order
        }
    )
    monkeypatch.setattr(
        executor_module,
        "_ensure_shared_clients",
        lambda: (breaker, tracker, ChaosController(redis_client)),
    )

    result = await execute_with_fallback(
        "write code",
        db_session,
        circuit_breaker=breaker,
        quota_tracker=tracker,
        chaos_controller=ChaosController(redis_client),
    )
    assert result.success is True
    assert result.model_used == top
    assert result.model_used != DEMO_PROVIDER_NAME


# --------------------------------------------------------------------------
# Explicit use through the API
# --------------------------------------------------------------------------


@pytest.fixture
async def client(monkeypatch, db_session, breaker, tracker, redis_client):
    from app.db.session import get_session

    async def override_session():
        yield db_session

    monkeypatch.setattr(app, "dependency_overrides", {get_session: override_session})
    monkeypatch.setattr(
        executor_module,
        "_ensure_shared_clients",
        lambda: (breaker, tracker, ChaosController(redis_client)),
    )
    # A real key would make the demo provider "unusable"; these tests exercise
    # the zero-key first run, which is the local-first default.
    for name in ("GROQ_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def test_forced_demo_route_returns_the_marked_answer(client):
    response = await client.post(
        "/api/v1/route",
        json={"prompt": "summarise this paragraph", "force_provider": "mock"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["model_used"] == DEMO_PROVIDER_NAME
    assert DEMO_MARKER in body["result"]


async def test_openai_model_mock_routes_to_the_demo_provider(client):
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "mock",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == DEMO_PROVIDER_NAME
    assert DEMO_MARKER in body["choices"][0]["message"]["content"]


async def test_openai_model_list_does_not_advertise_the_demo_model(client):
    body = (await client.get("/v1/models")).json()
    model_ids = [item["id"] for item in body["data"]]
    assert "routellm-auto" in model_ids
    assert DEMO_PROVIDER_NAME not in model_ids


async def test_setup_status_explains_the_first_run(client):
    body = (await client.get("/api/v1/setup/status")).json()
    assert body["local_mode"] is True
    assert body["require_api_key"] is False
    assert body["any_provider_configured"] is False
    assert body["demo_available"] is True
    assert body["demo_provider_name"] == DEMO_PROVIDER_NAME
    assert body["chaos_mode_enabled"] is settings.CHAOS_MODE_ENABLED
    # The reported driver must match the database actually in use. Asserting
    # "sqlite" outright breaks in CI, which sets DATABASE_URL so its Postgres
    # service is exercised — the local default is covered by
    # tests/unit/test_local.py, where the environment is controlled.
    if settings.DATABASE_URL.startswith("postgres"):
        # The driver name comes from the DSN: asyncpg for +asyncpg, psycopg...
        assert body["database_driver"] in {"asyncpg", "postgresql", "psycopg"}
    else:
        # SQLAlchemy's driver name for aiosqlite is "aiosqlite"; either
        # spelling proves the SQLite default rather than Postgres.
        assert body["database_driver"] in {"sqlite", "aiosqlite"}
    # Every real provider is listed with a way to get a free key.
    names = [p["name"] for p in body["providers"]]
    assert names == ["groq", "gemini", "deepseek"]
    for provider in body["providers"]:
        assert provider["configured"] is False
        assert provider["console_url"].startswith("https://")
        assert provider["free_note"]
    # The first incomplete step is the one to act on.
    assert body["preferred_provider"] == "groq"
    assert body["preferred_console_url"] == "https://console.groq.com/keys"
    assert [s["id"] for s in body["steps"]] == [
        "provider_key",
        "test_prompt",
        "try_failover",
    ]
    assert all(step["title"] for step in body["steps"])


async def test_setup_status_reports_a_saved_provider_key(client):
    saved = await client.put(
        "/api/v1/provider-keys/gemini", json={"api_key": "AIzaSyDUMMY_for_status"}
    )
    assert saved.status_code == 200

    body = (await client.get("/api/v1/setup/status")).json()
    gemini = next(p for p in body["providers"] if p["name"] == "gemini")
    assert gemini["configured"] is True
    assert gemini["key_source"] == "byok"
    assert body["any_provider_configured"] is True
    # Nothing secret is echoed back.
    assert "AIzaSyDUMMY_for_status" not in str(body)
    # With a real key present the demo path steps aside.
    assert body["demo_available"] is False
    # Action moves to the next unconfigured provider.
    assert body["preferred_provider"] == "groq"


async def test_setup_status_never_leaks_env_key_values(client, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-super-secret-value-for-detection")
    body = (await client.get("/api/v1/setup/status")).json()
    deepseek = next(p for p in body["providers"] if p["name"] == "deepseek")
    assert deepseek["configured"] is True
    assert deepseek["key_source"] == "env"
    assert "sk-super-secret-value-for-detection" not in str(body)
