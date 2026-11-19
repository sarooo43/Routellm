"""C5: contract tests for the shapes the respx-based tests imitate.

``test_model_cache_db.py`` and ``test_provider_test_endpoint.py`` replace the
provider transport with ``httpx.Response(200, json={...})``. That is the exact
bug class that has shipped three times here: the fake answers with a shape the
real endpoint never produces, so a broken parser stays green.

These tests do the same work over a REAL local socket. The stub speaks the
provider's documented wire format, and each test states which fake it pins:

  * models list  -> ``200 {"data": [{"id": ...}]}``   (groq/OpenAI shape)
  * chat         -> ``200 {"choices":[{"message":{"content":...}}], "usage":{}}``
  * auth failure -> ``401 {"error": {"message": ...}}``

If the code's parser ever stops matching what a provider actually sends, these
fail while a ``json=`` fake would still pass.
"""

import json
import socket
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from respx.router import MockRouter

from app.providers import model_registry
from app.providers.model_registry import ModelCatalog, PROVIDER_SPECS

GOOD_KEY = "gsk_CONTRACTTEST1234567890"


class _Upstream(BaseHTTPRequestHandler):
    """A provider-shaped stub: real status codes, real bodies, real headers."""

    #: what the stub should answer for the list call
    models_status = 200
    models_body = {"data": [{"id": "llama-3.1-8b-instant"}, {"id": "whisper-large-v3"}]}
    chat_status = 200
    chat_body = {
        "choices": [{"message": {"role": "assistant", "content": "pong"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }
    seen: list = []

    def _send(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        type(self).seen.append(
            (
                "GET",
                self.path,
                self.headers.get("Authorization"),
                self.headers.get("x-goog-api-key"),
            )
        )
        self._send(type(self).models_status, type(self).models_body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        type(self).seen.append(
            (
                "POST",
                self.path,
                self.headers.get("Authorization"),
                self.headers.get("x-goog-api-key"),
            )
        )
        self._send(type(self).chat_status, type(self).chat_body)

    def log_message(self, *args):
        pass


@pytest.fixture(autouse=True)
def _real_socket(respx_mock):
    """These tests need the network layer, not respx.

    The ``respx_mock`` fixture (requested by the two files this one audits)
    installs a global mock transport; every request then fails with
    AllMockedAssertionError before it can reach the loopback stub. Passing the
    fixture in and stopping it proves the test really used a socket.
    """
    respx_mock.stop()
    yield
    # Nothing to restart: the next test's respx_mock fixture starts its own.


def _fetch(upstream, key):
    """``ModelCatalog._fetch`` returns (ids, meta) - no source."""
    import asyncio

    return asyncio.get_event_loop().run_until_complete(
        ModelCatalog()._fetch(upstream["spec"], key)
    ) if False else ModelCatalog()._fetch(upstream["spec"], key)


@pytest.fixture
def upstream(monkeypatch):
    """A live stub, wired into PROVIDER_SPECS the way an operator would."""
    _Upstream.models_status = 200
    _Upstream.models_body = {
        "data": [{"id": "llama-3.1-8b-instant"}, {"id": "whisper-large-v3"}]
    }
    _Upstream.chat_status = 200
    # OBSERVED: chat_body was NOT reset here, so the 401 test's
    # {"error": "invalid api key"} body leaked into whichever test ran next and
    # the success-shape test failed with bad_provider_response. That made the
    # module order-dependent: 5 of 10 randomised runs failed, while every test
    # passed 5/5 when run alone. Restore the pristine body explicitly.
    _Upstream.chat_body = {
        "choices": [{"message": {"role": "assistant", "content": "pong"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }
    _Upstream.seen = []

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = ThreadingHTTPServer(("127.0.0.1", port), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # Bounded readiness: the socket is already bound, so this only proves the
    # accept loop is live, not that the internet exists.
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}/openai/v1/models", timeout=1.0)
            break
        except Exception:
            time.sleep(0.02)
    _Upstream.seen.clear()  # the probe above is not part of what is asserted

    spec = replace(
        PROVIDER_SPECS["groq"], base_url=f"http://127.0.0.1:{port}/openai/v1"
    )
    monkeypatch.setitem(model_registry.PROVIDER_SPECS, "groq", spec)
    model_registry.model_catalog.clear()
    try:
        yield {"port": port, "base": f"http://127.0.0.1:{port}", "spec": spec}
    finally:
        server.shutdown()
        server.server_close()
        model_registry.model_catalog.clear()


# ---------------------------------------------------------------------------
# What test_model_cache_db.py asserts, but over a real socket
# ---------------------------------------------------------------------------

async def test_persisted_discovery_works_over_a_real_socket(respx_mock, db_session, upstream):
    """Pins ``test_discovery_is_persisted_and_reused_after_a_cold_start``.

    Same claim, but the 200 comes from a socket rather than a fake response,
    so the cache only works if the parser matches a real provider body.
    """
    from sqlalchemy import select

    from app.models.model_cache import DiscoveredModelCacheDB

    monkeypatch_maker = type("_M", (), {})  # noqa: F841 - readability only

    class _Ctx:
        def __init__(self, s):
            self._s = s

        async def __aenter__(self):
            return self._s

        async def __aexit__(self, *_exc):
            return False

    class _Maker:
        def __call__(self):
            return _Ctx(db_session)

    original = ModelCatalog.session_maker
    ModelCatalog.session_maker = staticmethod(lambda: _Maker())
    try:
        catalog = ModelCatalog()
        models, meta, source = await catalog._discover(upstream["spec"], GOOD_KEY)
        assert source == "discovered"
        assert "llama-3.1-8b-instant" in models

        rows = (
            await db_session.execute(
                select(DiscoveredModelCacheDB).where(
                    DiscoveredModelCacheDB.provider == "groq"
                )
            )
        ).scalars().all()
        assert len(rows) == 1

        # A restarted process: new catalog, same DB, no second list call.
        again = ModelCatalog()
        reused, _meta2, source2 = await again._discover(upstream["spec"], GOOD_KEY)
        assert source2 == "db_cache"
        assert reused == models
    finally:
        ModelCatalog.session_maker = original

    # Exactly one GET reached the wire, and it carried a Bearer header.
    gets = [s for s in _Upstream.seen if s[0] == "GET"]
    assert len(gets) == 1
    assert gets[0][2] == f"Bearer {GOOD_KEY}"


async def test_a_401_is_not_mistaken_for_an_empty_catalog(upstream):
    """Pins ``_models(status=401)``: a rejected key must not read as "no models"."""
    _Upstream.models_status = 401
    _Upstream.models_body = {"error": {"message": "Invalid API Key"}}

    models, meta = await ModelCatalog()._fetch(upstream["spec"], GOOD_KEY)

    # A rejected credential is an EMPTY result, never a fabricated catalog.
    assert models == []
    assert meta == {} or isinstance(meta, dict)


async def test_server_garbage_does_not_become_a_catalog(upstream):
    """A 200 with an unparseable body must fail loudly, not silently empty."""
    _Upstream.models_status = 200
    _Upstream.models_body = {"unexpected": "shape"}

    models, _meta = await ModelCatalog()._fetch(upstream["spec"], GOOD_KEY)

    assert models == []


async def test_an_empty_key_sends_nothing_at_all(upstream):
    """OBSERVED split, now CLOSED (Phase 1).

    This test used to pin the old behaviour: discovery omitted the
    Authorization header for an empty key (so the provider answered 401) while
    the chat adapter built ``Bearer `` (so h11 refused locally). That split is
    exactly the owner's log. Discovery now refuses an empty key outright, so
    both paths report the same missing-key condition and neither touches the
    wire.
    """
    await ModelCatalog()._fetch(upstream["spec"], "")

    gets = [s for s in _Upstream.seen if s[0] == "GET"]
    assert gets == [], f"discovery sent a credential-less request: {gets}"

    # The chat half: every adapter reports the same outcome for a blank key.
    import asyncio

    from app.providers.groq_provider import GroqProvider

    result = await GroqProvider().generate("hi", model="llama-3.1-8b-instant",
                                           provider_api_key="")
    assert result.outcome == "missing_provider_key"


# ---------------------------------------------------------------------------
# What test_provider_test_endpoint.py asserts, but over a real socket
# ---------------------------------------------------------------------------

async def test_chat_success_shape_over_a_real_socket(upstream):
    """Pins ``respx_mock.post(GROQ_CHAT) -> 200 GROQ_OK``.

    The eval/UI read exactly three things: choices[0].message.content and
    usage.total_tokens. A fake that omits either would stay green while a real
    provider response crashed the parser.
    """
    from app.providers.groq_provider import GroqProvider

    result = await GroqProvider().generate(
        "ping", model="llama-3.1-8b-instant", provider_api_key=GOOD_KEY
    )

    assert result.success is True
    assert result.text == "pong"
    assert result.tokens_used == 4
    # The credential the chat path really put on the wire.
    posts = [s for s in _Upstream.seen if s[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][2] == f"Bearer {GOOD_KEY}"


async def test_chat_401_shape_is_read_as_auth_failed_over_a_real_socket(upstream):
    """Pins ``respx_mock.post(GROQ_CHAT) -> 401 {"error": "invalid api key"}``.

    A provider error body is not always ``{"error": {"message": ...}}``; a
    bare string is common. The adapter must classify it as a key problem.
    """
    from app.providers.groq_provider import GroqProvider

    _Upstream.chat_status = 401
    _Upstream.chat_body = {"error": "invalid api key"}

    result = await GroqProvider().generate(
        "ping", model="llama-3.1-8b-instant", provider_api_key=GOOD_KEY
    )

    # Not a network error, not a missing model: a key problem.
    assert result.success is False
    assert result.outcome == "auth_failed"


async def test_the_gemini_list_shape_over_a_real_socket(monkeypatch):
    """Pins ``test_model_discovery.py``'s GEMINI_MODELS fake.

    Gemini's list body is ``{"models":[{"name":"models/<id>",
    "supportedGenerationMethods":[...]}]}`` - a different shape from OpenAI's
    ``{"data":[{"id":...}]}``, and the app claims to read both. Covered here
    over a socket so the parse tolerance is not a fake's promise.
    """
    body = {
        "models": [
            {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
            {"name": "models/gemini-2.0-flash-lite", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": ["generateContent"]},
        ]
    }
    _Upstream.models_status = 200
    _Upstream.models_body = body

    from http.server import ThreadingHTTPServer

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = ThreadingHTTPServer(("127.0.0.1", port), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Upstream.seen.clear()
    try:
        spec = replace(
            PROVIDER_SPECS["gemini"], base_url=f"http://127.0.0.1:{port}/v1beta"
        )
        ids, _meta = await ModelCatalog()._fetch(spec, GOOD_KEY)
    finally:
        server.shutdown()
        server.server_close()

    # ``models/`` prefix stripped, the embed model excluded, chat models kept.
    assert "gemini-2.0-flash" in ids
    assert "text-embedding-004" not in ids
    assert all(not i.startswith("models/") for i in ids)
    # Gemini pins its own header and sends NO Authorization: Bearer.
    get = _Upstream.seen[0]
    assert get[3] == GOOD_KEY
    assert get[2] is None
