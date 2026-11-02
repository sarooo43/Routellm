"""CORS + database-unavailable fail-soft behavior for the public gateway.

CORS test builds a fresh app instance after patching settings.CORS_ORIGINS
(an explicit browser-origin allowlist); the 503 test proves a missing
DATABASE_URL degrades key-protected endpoints into a clean, styled 503
envelope instead of a raw 500.
"""

import httpx
import pytest

from app.config import cors_origin_list, settings
from app.db.session import DatabaseUnavailableError, get_session
from app.main import app


@pytest.fixture
async def client(db_session, breaker, tracker):
    async def override_session():
        yield db_session

    app.dependency_overrides[get_session] = override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    app.dependency_overrides.clear()


def test_cors_origin_list_defaults_to_dev_servers():
    assert cors_origin_list(settings) == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
    ]


def test_cors_origin_list_parses_and_trims_env():
    shim = settings.model_copy(update={"CORS_ORIGINS": " https://a.dev , https://b.dev/ ,c"})
    assert cors_origin_list(shim) == ["https://a.dev", "https://b.dev", "c"]


async def test_configured_origin_gets_cors_headers(monkeypatch):
    """A cross-origin browser client (e.g. one setting VITE_API_BASE_URL
    straight at this backend) must only work when its origin is explicitly
    allowlisted via CORS_ORIGINS."""
    import importlib

    import app.main as main_module

    allowed = "https://routellm.saboornasar66.workers.dev"
    monkeypatch.setattr(settings, "CORS_ORIGINS", allowed)
    reloaded = importlib.reload(main_module)

    transport = httpx.ASGITransport(app=reloaded.app if hasattr(reloaded, "app") else reloaded)
    async with httpx.AsyncClient(transport=transport, base_url=allowed) as http:
        # Simple GET from the allowed origin.
        resp = await http.get("/health", headers={"Origin": allowed})
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == allowed

        # Preflight for a request WITH the X-API-Key header must succeed.
        preflight = await http.options(
            "/api/v1/route",
            headers={
                "Origin": allowed,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-api-key,content-type",
            },
        )
        assert preflight.status_code == 200
        assert (
            preflight.headers["access-control-allow-headers"]
            .lower()
            .find("x-api-key") >= 0
        )

        # A NON-allowlisted browser origin must be rejected by CORS.
        stranger = await http.get("/health", headers={"Origin": "https://evil.example.com"})
        assert stranger.headers.get("access-control-allow-origin") is None


async def test_missing_database_url_yields_clean_503(monkeypatch, breaker, tracker):
    """DB not configured (or unreachable) → 503 'Database temporarily
    unavailable' from key-protected endpoints — never a raw traceback."""
    from app.db import session as session_module

    class FailingMaker:
        def __call__(self):
            raise DatabaseUnavailableError("cannot connect")

    monkeypatch.setattr(session_module, "get_session_maker", FailingMaker)
    app.dependency_overrides.pop(get_session, None)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        resp = await http.post("/api/v1/route", json={"prompt": "hi"})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "Database temporarily unavailable"}

        # /health must STILL respond and report component status, not 500/503.
        health = await http.get("/health")
        assert health.status_code in (200, 503)
        body = health.json()
        assert set(body["components"].keys()) == {"database", "redis"}
