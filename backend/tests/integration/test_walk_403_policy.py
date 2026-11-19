"""T4: what the Auto walk does with a 403, through the real app.

OBSERVED LIVE: Gemini's model LIST call succeeded (the key can enumerate
models) but the chat call returned 403 PERMISSION_DENIED, and the log showed
exactly ONE chat attempt for that provider.

Rule in the code (app/core/model_discovery.py:771-777) and in docs/field-reality:
a 403 is treated as the KEY being refused rather than the model string, so the
walk tries ONE candidate from a DIFFERENT model family as a probe, then stops.
It is never cached as model-denied. That is why a single attempt per provider
is expected when the discovered list has only one family.

These tests assert that documented rule end to end, so nobody "fixes" the
one-attempt behaviour by accident - and so the cross-family probe keeps
working.
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

#: The discovered model list the stub serves. The owner's Gemini account listed
#: only gemini-* ids; a second family (gemma) is used to prove the probe rule.
_MODELS_ONE_FAMILY = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-3.8-flash"]
_MODELS_TWO_FAMILIES = ["gemini-2.5-flash", "gemini-2.5-pro", "gemma-4-31b-it"]
_MODELS = list(_MODELS_ONE_FAMILY)


class _Geminiish(BaseHTTPRequestHandler):
    """List call succeeds; chat calls answer 403 for EVERY model.

    That is the owner's observed shape: enumeration allowed, generation
    refused.
    """

    chat_calls: list[str] = []

    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        # Real Gemini-shaped ids: family = the vendor token before the first
        # dash ("gemini" / "gemma"), which is what the probe rule groups by.
        # The owner's shape: ONE family (all gemini-*). The documented probe
        # rule looks for a DIFFERENT family, finds none, and stops - which is
        # exactly why the live log showed a single attempt for Gemini.
        self._reply(200, {"data": [{"id": "gemini-2.5-flash", "owned_by": "g"},
                                   {"id": "gemini-2.5-pro", "owned_by": "g"},
                                   {"id": "gemini-3.8-flash", "owned_by": "g"}]})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            model = json.loads(raw or b"{}").get("model", "?")
        except Exception:
            model = "?"
        _Geminiish.chat_calls.append(model)
        self._reply(403, {"error": {"message": "Permission denied on the service.",
                                    "code": 403}})

    def log_message(self, *a):
        pass


async def _purge_db_model_cache(db_session):
    """Discovery has a DB tier; the process-global catalog is not enough.

    Without this the first attempt of the next test is served from the
    PREVIOUS test's model list (a warm-cache hit), which silently defeats any
    assertion about which models the walk tries.
    """
    from sqlalchemy import delete

    from app.models.model_cache import DiscoveredModelCacheDB

    await db_session.execute(delete(DiscoveredModelCacheDB))
    await db_session.commit()


@pytest.fixture(scope="module")
def upstream():
    _Geminiish.chat_calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Geminiish)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(autouse=True)
def _repoint(monkeypatch, upstream):
    from dataclasses import replace

    monkeypatch.setitem(
        PROVIDER_SPECS,
        "groq",
        replace(PROVIDER_SPECS["groq"], base_url=f"{upstream}/openai/v1"),
    )
    monkeypatch.setattr(settings, "GROQ_MODEL", None)
    monkeypatch.setattr(settings, "GROQ_API_KEY", None)
    _Geminiish.chat_calls = []
    globals()["_MODELS"] = list(_MODELS_ONE_FAMILY)
    # Fresh discovery: no warm cache from another test. The catalog outlives a
    # test (it is process-global and DB-backed), so without this the first
    # attempt of the NEXT test is served from the previous test's model list.
    from app.providers.model_registry import model_catalog

    model_catalog.clear()


@pytest.fixture(autouse=True)
def _encryption(monkeypatch):
    monkeypatch.setattr(
        settings, "PROVIDER_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )


@pytest.fixture(autouse=True)
def _no_background_discovery(monkeypatch):
    import app.api.provider_keys_routes as routes

    async def _noop(provider_name, raw_key):
        return None

    monkeypatch.setattr(routes, "_warm_discovery_after_save", _noop)


@pytest.fixture
async def client(monkeypatch, db_session, breaker, tracker, redis_client):
    from app.core import executor as executor_module
    from app.core.chaos import ChaosController
    from app.api.dependencies import require_provider_api_keys
    from app.db.session import get_session

    async def override_session():
        yield db_session

    async def _stored_key():
        return {"groq": GOOD_KEY}

    existing = dict(app.dependency_overrides)
    monkeypatch.setattr(
        app, "dependency_overrides", {**existing, get_session: override_session}
    )
    app.dependency_overrides[require_provider_api_keys] = _stored_key
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


async def _route(client, db_session=None):
    if db_session is not None:
        await _purge_db_model_cache(db_session)
    meta = (await client.post("/api/v1/keys/generate", json={"label": "o"})).json()
    return await client.post(
        "/api/v1/route",
        json={
            "prompt": "hello",
            "task_type": "general",
            "force_provider": "groq",
            "max_fallbacks": 1,
        },
        headers={"X-API-Key": meta["api_key"]},
    )


async def test_one_family_means_exactly_one_attempt(client, db_session):
    """The owner's observation, reproduced and pinned.

    Documented rule (model_discovery.py:771-779): on a 403, look for ONE model
    from a DIFFERENT family as a probe. When every discovered model is the same
    family - which is the case for a Gemini list of gemini-* ids - there is no
    probe to make, so the walk stops after a single attempt. Three models were
    discovered; only one may be refused.
    """
    body = (await _route(client, db_session)).json()

    assert body["success"] is False
    assert len(_Geminiish.chat_calls) == 1, (
        "a refused key was retried within the same family: "
        f"{_Geminiish.chat_calls}"
    )


async def test_403_is_reported_as_auth_failed_not_as_a_missing_model(client, db_session):
    """A refused credential must never be described as a bad model id."""
    body = (await _route(client, db_session)).json()
    error = (body.get("error") or "").lower()

    assert "permission" in error or "auth" in error or "403" in error, error
    assert "model_not_found" not in error, error


async def test_403_is_not_cached_as_model_denied(client, db_session):
    """The docstring is explicit: a 403 is the KEY, not the model.

    Caching it as model-denied would silently drop working models for 24h.
    """
    from app.core.model_discovery import denied_models

    await _route(client, db_session)
    assert denied_models(GOOD_KEY, "groq") == set(), (
        "a 403 was cached as a model-denied status"
    )
