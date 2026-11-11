"""Auth-surface matrix: which endpoints are public vs identity-gated.

Pins the contract so a future change can't accidentally expose (or lock up)
endpoints. Every request here is made WITHOUT an X-API-Key header unless the
test adds one.

Public (2xx without a key in BOTH modes): /health, /api/v1/catalog,
                              /api/v1/models, /api/v1/analytics,
                              /api/v1/logs, /api/v1/keys/generate,
                              /v1/models, /v1/models oracle

Identity-gated endpoints under REQUIRE_API_KEY=true behave like a closed
system: 401 without a key. Under REQUIRE_API_KEY=false (the local-first
default) they run under a default local identity instead of 401-ing:

- POST /api/v1/route, POST /v1/chat/completions → NOT 401 (they proceed;
  with no provider keys configured the business logic reports its own
  clean failure, never an auth wall);
- GET /api/v1/keys/me, GET /api/v1/provider-keys → operate on the local
  identity (2xx for valid operations);
- an INVALID X-API-Key is still 401 even with auth off, so a typo can never
  silently impersonate the default identity.
"""

import httpx
import pytest

from app.config import settings
from app.main import app

PUBLIC_PATHS = (
    ("get", "/health", 200),
    ("get", "/api/v1/catalog", 200),
    ("get", "/api/v1/models", 200),
    ("get", "/api/v1/analytics", 200),
    ("get", "/api/v1/logs", 200),
    ("get", "/v1/models", 200),
)


async def _make_client(db_session, breaker, tracker):
    from app.db.session import get_session

    async def override_session():
        yield db_session

    app.dependency_overrides[get_session] = override_session
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.fixture(params=[True, False], ids=["auth_required", "auth_optional"])
async def client(request, monkeypatch, db_session, breaker, tracker):
    auth_required = request.param
    monkeypatch.setattr(settings, "REQUIRE_API_KEY", auth_required)
    from app.db.session import get_session

    async def override_session():
        yield db_session

    app.dependency_overrides[get_session] = override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac, auth_required
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# Public endpoints — must work with NO API key in both auth modes.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,expected", [(m, p, e) for (m, p, e) in PUBLIC_PATHS]
)
async def test_public_paths_work_keyless_in_both_modes(client, method, path, expected):
    http, _ = client
    resp = await http.request(method, path)
    assert resp.status_code == expected, f"{method} {path}"


@pytest.mark.parametrize(
    "method,path,expected", [(("post", "/api/v1/keys/generate", 201))]
)
async def test_keys_generate_is_public_but_201_in_both_modes(client, method, path, expected):
    http, _ = client
    resp = await http.post("/api/v1/keys/generate", json={"label": "matrix"}, timeout=None)
    assert resp.status_code == expected
    assert resp.json()["api_key"].startswith("rk_")
    await http.aclose()


# --------------------------------------------------------------------------
# Identity-gated endpoints: mode-dependent expectations, pinned per mode.
# --------------------------------------------------------------------------


async def test_identity_gated_endpoints_respect_the_mode(client):
    http, auth_required = client
    commands = {
        "post /api/v1/route": lambda h: h.post("/api/v1/route", json={"prompt": "hi"}),
        "post /v1/chat/completions": lambda h: h.post(
            "/v1/chat/completions",
            json={
                "model": "routellm-auto",
                "messages": [{"role": "user", "content": "hi"}],
            },
        ),
        "get /api/v1/keys/me": lambda h: h.get("/api/v1/keys/me"),
        "get /api/v1/provider-keys": lambda h: h.get("/api/v1/provider-keys"),
    }
    for label, call in commands.items():
        resp = await call(http)
        if auth_required:
            assert resp.status_code == 401, f"{label}: expected 401 in required mode"
        else:
            assert (
                resp.status_code != 401
            ), f"{label}: optional mode must not present an auth wall to keyless requests"


async def test_auth_optional_local_identity_serves_account_surface(client, db_session):
    from sqlmodel import select

    from app.models import ApiKeyDB

    http, auth_required = client
    if auth_required:
        pytest.skip("only meaningful in optional-auth (local identity) mode")

    # Local identity GET /api/v1/keys/me → 200 with that identity's stats.
    resp = await http.get("/api/v1/keys/me")
    assert resp.status_code == 200

    # Provider-key surface operates on the local identity's rows.
    listing = await http.get("/api/v1/provider-keys")
    assert listing.status_code == 200

    rows = (await db_session.execute(select(ApiKeyDB))).scalars().all()
    assert any(row.key_prefix == "local-default" for row in rows)


# --------------------------------------------------------------------------
# An INVALID key is rejected even with auth off (typo != local identity).
# --------------------------------------------------------------------------


async def test_invalid_key_is_rejected_even_with_auth_off(client):
    http, auth_required = client
    if auth_required:
        pytest.skip("covered by the required-mode 401 matrix above")
    resp = await http.post(
        "/api/v1/route",
        json={"prompt": "hi"},
        headers={"X-API-Key": "rk_not_a_real_key_value"},
    )
    assert resp.status_code == 401
