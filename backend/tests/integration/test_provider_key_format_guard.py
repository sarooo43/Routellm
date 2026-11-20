"""A saved provider key that cannot be sent must never look like a network error.

OBSERVED LIVE (owner's server log, three real provider keys):
`provider_attempt_failed_continuing` reported
``network_error: the request to api.groq.com failed before a response
(LocalProtocolError)`` after only ~37 ms. Nothing was sent and nothing was
received - httpx/h11 refused to serialise the ``Authorization`` header because
the saved key contained a character that is illegal in a header value.

The model-LIST call for the same key came back 401 in the owner's log. An
earlier explanation for that split (a different HTTP stack trimming the value)
is RETRACTED: both paths use httpx and build the header identically, and every
control character makes both raise. The owner's split remains UNEXPLAINED; what
these tests cover is the part this repo is actually responsible for - that an
unsendable value is refused on save and can never crash a request.

These tests go through the real app (ASGITransport), save through the real
PUT /api/v1/provider-keys/{provider}, and never mock our own adapter. The
upstream is a real local HTTP server reached through a repointed base_url, so
httpx builds, encodes and sends the real request.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.main import app
from app.providers.model_registry import PROVIDER_SPECS

GOOD_KEY = "gsk_TESTKEY1234567890abcdef"


class _Upstream(BaseHTTPRequestHandler):
    """A provider that answers 200 for the good key and 401 for anything else."""

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.headers.get("Authorization") == f"Bearer {GOOD_KEY}":
            self._reply(200, {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"total_tokens": 3},
            })
        else:
            self._reply(401, {"error": {"message": "Invalid API Key"}})

    def do_GET(self):  # noqa: N802 - http.server API
        if self.headers.get("Authorization") == f"Bearer {GOOD_KEY}":
            self._reply(200, {"data": [{"id": "test-model", "owned_by": "test"}]})
        else:
            self._reply(401, {"error": {"message": "Invalid API Key"}})

    def log_message(self, *args):  # silence the test output
        pass


@pytest.fixture(scope="module")
def upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(autouse=True)
def _repoint_groq(monkeypatch, upstream):
    """Point ONLY the host at the stub. The adapter still builds the real
    request (url, headers, body); httpx still encodes and sends it."""
    from dataclasses import replace

    monkeypatch.setitem(
        PROVIDER_SPECS,
        "groq",
        replace(PROVIDER_SPECS["groq"], base_url=f"{upstream}/openai/v1"),
    )
    monkeypatch.setattr(settings, "GROQ_MODEL", None)


@pytest.fixture(autouse=True)
def _encryption(monkeypatch):
    monkeypatch.setattr(
        settings, "PROVIDER_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )


@pytest.fixture(autouse=True)
def _no_background_discovery(monkeypatch):
    """The save path fires a background list call; keep tests off the wires
    they do not assert about."""
    import app.api.provider_keys_routes as routes

    async def _noop(provider_name, raw_key):
        return None

    monkeypatch.setattr(routes, "_warm_discovery_after_save", _noop)


@pytest.fixture
async def client(monkeypatch, db_session, breaker, tracker, redis_client):
    from app.core import executor as executor_module
    from app.core.chaos import ChaosController
    from app.db.session import get_session

    async def override_session():
        yield db_session

    existing = dict(app.dependency_overrides)
    monkeypatch.setattr(app, "dependency_overrides", {**existing, get_session: override_session})
    monkeypatch.setattr(settings, "REQUIRE_API_KEY", False)
    monkeypatch.setattr(
        executor_module,
        "_ensure_shared_clients",
        lambda: (breaker, tracker, ChaosController(redis_client)),
    )
    import app.api.chaos_routes as chaos_routes_module

    monkeypatch.setattr(
        chaos_routes_module,
        "_ensure_shared_clients",
        lambda: (breaker, tracker, ChaosController(redis_client)),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:8000", timeout=30.0
    ) as ac:
        yield ac


async def _key(client) -> str:
    resp = await client.post("/api/v1/keys/generate", json={"label": "owner"})
    assert resp.status_code == 201, resp.text
    return resp.json()["api_key"]


async def _save(client, key: str, provider: str = "groq"):
    return await client.put(
        f"/api/v1/provider-keys/{provider}",
        json={"api_key": key},
        headers={"X-API-Key": await _key(client)},
    )


async def _route(client, api_key: str, provider: str = "groq"):
    return await client.post(
        "/api/v1/route",
        json={
            "prompt": "hello",
            "task_type": "general",
            "force_provider": provider,
            "max_fallbacks": 1,
        },
        headers={"X-API-Key": api_key},
    )


# ---------------------------------------------------------------------------
# T5: one test per variant from the reproduction table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label,key",
    [
        ("normal", GOOD_KEY),
        ("trailing_space", GOOD_KEY + " "),
        ("trailing_newline", GOOD_KEY + "\n"),
        ("leading_tab", "\t" + GOOD_KEY),
    ],
)
async def test_sendable_keys_are_accepted_and_stripped(client, label, key):
    """Edges are trimmed, so a copy-paste space or newline just works."""
    resp = await _save(client, key)
    assert resp.status_code == 200, f"{label}: {resp.text}"
    assert resp.json()["configured"] is True


@pytest.mark.parametrize(
    "label,key,expected_fragment",
    [
        ("embedded_newline", GOOD_KEY[:10] + "\n" + GOOD_KEY[10:], "newline"),
        ("embedded_cr", GOOD_KEY[:10] + "\r" + GOOD_KEY[10:], "carriage return"),
        ("non_ascii", GOOD_KEY + "é", "non-ASCII"),
        ("nul", GOOD_KEY[:10] + "\x00" + GOOD_KEY[10:], "control character"),
    ],
)
async def test_unusable_keys_are_rejected_on_save_without_echoing_them(
    client, label, key, expected_fragment
):
    """A key that cannot be sent is refused where the mistake was made."""
    resp = await _save(client, key)

    assert resp.status_code == 422, f"{label} was accepted: {resp.text}"
    detail = resp.json()["detail"]
    assert expected_fragment in detail
    assert "position" in detail, detail
    # The error must describe the problem, never reproduce the secret.
    assert GOOD_KEY not in detail
    assert key.strip() not in detail
    assert key not in resp.text


async def test_a_stored_bad_key_degrades_truthfully_not_as_network_error(
    client, monkeypatch
):
    """REQUEST-TIME safety: keys saved before save-time validation existed.

    Simulated by writing an unusable value straight past the endpoint (the
    stored-key reality), then routing. The result must name the key format and
    must NOT be network_error - nothing was sent, so nothing about the network
    can be true.
    """
    owner_key = await _key(client)
    # The /route handler resolved this dependency at import time
    # (app/api/routes.py: `from app.api.dependencies import
    # require_provider_api_keys`), so the override must target
    # app.dependency_overrides with the same function object - not a patched
    # attribute on some other module.
    from app.api.dependencies import require_provider_api_keys

    async def _stored_bad_key():
        return {"groq": BAD_KEY}

    app.dependency_overrides[require_provider_api_keys] = _stored_bad_key

    resp = await _route(client, owner_key)
    body = resp.json()

    assert resp.status_code == 200
    assert body["success"] is False
    error = body.get("error") or ""
    assert "network_error" not in error, error
    assert "invalid_key_format" in error, error
    assert "re-save" in error.lower()
    # No secret, in any form, anywhere in the response.
    assert BAD_KEY not in resp.text
    assert BAD_KEY[:12] not in resp.text


BAD_KEY = GOOD_KEY[:10] + "\n" + GOOD_KEY[10:]


async def test_request_time_guard_stops_a_key_stored_before_validation(
    client, db_session, upstream
):
    """The stale-storage case, exercised WITHOUT the save endpoint.

    A row written by an older build (or any non-endpoint writer) still holds an
    unsendable value. The request-time guard must catch it, so the failure is
    named as a key-format problem and no socket is opened: the stub upstream
    must receive nothing.
    """
    from app.core.provider_key_manager import upsert_provider_key

    owner_key = await _key(client)
    owner_id = (
        await client.get("/api/v1/keys/me", headers={"X-API-Key": owner_key})
    ).json()
    assert owner_id["key_prefix"]

    # Write straight to storage, bypassing normalize() - that is the point.
    from sqlalchemy import select

    from app.models.api_key import ApiKeyDB

    row = (
        await db_session.execute(
            select(ApiKeyDB).where(ApiKeyDB.is_active == True)  # noqa: E712
        )
    ).scalars().first()
    await upsert_provider_key(db_session, row.id, "groq", BAD_KEY)
    await db_session.commit()

    resp = await _route(client, owner_key)
    body = resp.json()
    error = body.get("error") or ""

    assert body["success"] is False
    assert "invalid_key_format" in error, error
    assert "network_error" not in error, error
    assert BAD_KEY not in resp.text
