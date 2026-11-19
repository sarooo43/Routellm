"""PHASE 1: an empty provider key must never look like a network problem.

OBSERVED on the owner's real server, one POST /api/v1/route:

    groq/deepseek: discovery "model_discovery_rejected" 401,
                   then chat "network_error ... (LocalProtocolError)" in ~36 ms
    gemini:        discovery 403, chat "auth_failed ... 403 PERMISSION_DENIED"

The mechanism, reproduced here on a real uvicorn server with a local stub:
an EMPTY key was serialised as ``Authorization: Bearer `` - a header value h11
refuses to write - so chat died locally while discovery sent a
credential-less request (``_auth_headers`` omits the header for a falsy key)
that the provider answered 401. One missing key produced two different
failures, neither of which named the missing key.

These tests pin the corrected behaviour from the outside: no request on the
wire, one truthful diagnosis, and the same diagnosis from discovery and chat.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FAKE = "gsk_FAKE0000000000000000000000"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Unit level: the rule itself
# ---------------------------------------------------------------------------

def test_an_empty_key_is_not_sendable():
    """The regression: ``describe_unusable("")`` used to return None = "fine",
    so every request-time guard let it through."""
    from app.core.provider_key_format import describe_unusable, is_sendable

    for blank in ("", "   ", "\t", "\n", None):
        assert describe_unusable(blank) is not None, f"{blank!r} reported sendable"
        assert is_sendable(blank) is False
    # Edge whitespace is still only trimmed, never an error.
    assert is_sendable("gsk_abc ") is True


async def test_all_three_adapters_agree_about_a_missing_key():
    """Groq and DeepSeek shared a copied check; Gemini had none at all."""
    from app.providers.deepseek_provider import DeepSeekProvider
    from app.providers.gemini_provider import GeminiProvider
    from app.providers.groq_provider import GroqProvider

    cases = {
        GroqProvider: "llama-3.1-8b-instant",
        DeepSeekProvider: "deepseek-chat",
        GeminiProvider: "gemini-2.0-flash",
    }
    for provider_cls, model in cases.items():
        for blank in ("", "   "):
            result = await provider_cls().generate(
                "hi", provider_api_key=blank, model=model
            )
            assert result.success is False
            assert result.outcome == "missing_provider_key", (
                f"{provider_cls.__name__} with {blank!r} said {result.outcome!r}"
            )
            assert "network" not in (result.error or "").lower()


async def test_an_unsendable_key_is_a_format_problem_not_a_missing_one():
    from app.providers.groq_provider import GroqProvider

    result = await GroqProvider().generate(
        "hi", provider_api_key="gsk_a\nb", model="llama-3.1-8b-instant"
    )
    assert result.success is False
    assert result.outcome == "invalid_key_format"


async def test_discovery_refuses_an_empty_key_without_a_request():
    """Same condition, same verdict: discovery must not send a
    credential-less request that comes back 401.

    The stub must be REACHABLE and still receive nothing. Pointing at a dead
    port instead would pass whether the guard existed or not - a connection
    error and a refusal both yield ``([], {})`` - which is exactly the kind of
    assertion that hides a regression (mutation-tested).
    """
    from app.providers.model_registry import ModelCatalog, PROVIDER_SPECS

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    stub = ThreadingHTTPServer(("127.0.0.1", port), _Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    _Stub.calls = []
    try:
        spec = replace(PROVIDER_SPECS["groq"], base_url=f"http://127.0.0.1:{port}/v1")
        for blank in ("", "   ", None):
            ids, meta = await ModelCatalog()._fetch(spec, blank)
            assert (ids, meta) == ([], {})
        assert _Stub.calls == [], f"discovery sent a request anyway: {_Stub.calls}"
    finally:
        stub.shutdown()
        stub.server_close()


# ---------------------------------------------------------------------------
# Explainability: the message must not blame the gateway or the network
# ---------------------------------------------------------------------------

def test_the_router_explains_a_key_problem_as_a_key_problem():
    from app.core.error_messages import explain

    missing = explain(
        "missing_provider_key: no groq key is saved for this Routtener key.", "groq"
    )
    assert missing.startswith("missing_provider_key:")
    assert "unexpected error" not in missing

    auth = explain("auth_failed: groq rejected the credential (HTTP 401).", "groq")
    assert auth.startswith("auth_failed:")
    assert "unexpected error" not in auth

    fmt = explain("invalid_key_format: the saved groq key is empty, so no request could be sent.", "groq")
    assert fmt.startswith("invalid_key_format:")
    assert "unexpected error" not in fmt


def test_a_local_protocol_error_is_never_called_a_network_failure():
    """h11 refusing to serialise OUR header means a value we produced is
    unsendable. "Check your internet" is the wrong diagnosis."""
    from app.core.error_messages import explain

    text = explain(
        "network_error: the request to api.groq.com failed before a response "
        "(LocalProtocolError).",
        "groq",
    )
    assert text.startswith("invalid_key_format:")
    assert "internet" not in text.lower()


def test_real_network_failures_are_still_network_failures():
    """The narrow pass-through must not swallow genuine connectivity errors."""
    from app.core.error_messages import explain

    for raw in ("ConnectError", "TimeoutException", "network_error: could not open a connection"):
        text = explain(raw, "groq")
        assert text.startswith(("network_unreachable:", "network_error:")), (raw, text)


# ---------------------------------------------------------------------------
# End to end: the owner's scenario on a real server
# ---------------------------------------------------------------------------

class _Stub(BaseHTTPRequestHandler):
    """A provider that answers 401 to a credential-less request."""

    calls: list = []

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).calls.append(("GET", self.headers.get("Authorization")))
        self._send(401, {"error": {"message": "Invalid API Key"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        type(self).calls.append(("POST", self.headers.get("Authorization")))
        self._send(401, {"error": {"message": "Invalid API Key"}})

    def log_message(self, *args):
        pass


@pytest.fixture
def live(monkeypatch, tmp_path):
    """Real uvicorn + real SQLite + local provider stub, empty env key."""
    _Stub.calls = []
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        up = probe.getsockname()[1]
    stub = ThreadingHTTPServer(("127.0.0.1", up), _Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    import app.providers.model_registry as registry

    monkeypatch.setitem(
        registry.PROVIDER_SPECS,
        "groq",
        replace(registry.PROVIDER_SPECS["groq"], base_url=f"http://127.0.0.1:{up}/v1"),
    )
    monkeypatch.setenv("GROQ_API_KEY", "")

    from app.main import app as app_obj

    port = _free_port()
    env = dict(os.environ)
    env.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'k.db'}",
            "REDIS_URL": "",
            "DEMO_MODE_ENABLED": "false",
            "CHAOS_MODE_ENABLED": "false",
            "REQUIRE_API_KEY": "false",
            "ALLOWED_HOSTS": "*",
            "GROQ_API_KEY": "",
            "PYTHONPATH": str(REPO_ROOT / "backend"),
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "critical"],
        cwd=REPO_ROOT / "backend", env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server died: {proc.communicate()[0]}")
        try:
            if httpx.get(f"{base}/health", timeout=1.0).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        proc.kill()
        raise AssertionError("server never became ready")

    try:
        yield {"base": base, "stub": stub}
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            proc.communicate()
        stub.shutdown()
        stub.server_close()


def test_the_owner_split_is_gone_end_to_end(live):
    """OBSERVED before the fix: discovery wire call with NO auth header,
    chat LocalProtocolError, two different stories for one absent key."""
    with httpx.Client(base_url=live["base"], timeout=30.0) as c:
        key = c.post("/api/v1/keys/generate", json={"label": "o"}).json()["api_key"]
        headers = {"X-API-Key": key}

        _Stub.calls.clear()
        discovery = c.get(
            "/api/v1/provider-keys/groq/models?refresh=true", headers=headers
        )
        route = c.post(
            "/api/v1/route",
            json={"prompt": "hi", "task_type": "general",
                  "force_provider": "groq", "max_fallbacks": 0},
            headers=headers,
        )

    assert discovery.status_code in (200, 404)
    # Nothing reached the provider: no credential-less GET, no "Bearer ".
    assert _Stub.calls == [], f"an empty key still hit the wire: {_Stub.calls}"

    body = route.json()
    assert body["success"] is False
    assert body["error"].startswith("missing_provider_key:"), body["error"]
    assert "unexpected error" not in body["error"]
    assert "network" not in body["error"].lower()


def test_the_dashboard_does_not_call_an_unusable_key_configured(live, tmp_path):
    """1d: ``configured: true`` next to a green badge, for a key that cannot
    be used. A row is not a key.

    Two states that used to both read as configured: a ciphertext that cannot
    be decrypted at all, and one that decrypts to an empty string.
    """
    import sqlite3

    from app.core.provider_key_crypto import _fernet

    db = Path(tmp_path / "k.db")
    with httpx.Client(base_url=live["base"], timeout=30.0) as c:
        key = c.post("/api/v1/keys/generate", json={"label": "o"}).json()["api_key"]
        headers = {"X-API-Key": key}
        saved = c.put(
            "/api/v1/provider-keys/groq", json={"api_key": FAKE}, headers=headers
        )
        assert saved.status_code == 200
        assert saved.json()["configured"] is True

        def status_for(name: str) -> dict:
            rows = c.get("/api/v1/provider-keys", headers=headers).json()
            return next(r for r in rows if r["provider_name"] == name)

        assert status_for("groq")["configured"] is True
        assert status_for("groq")["key_hint"] == "****0000"

        # (a) the ciphertext cannot be decrypted (truncated / wrong key file)
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE provider_keys SET encrypted_key='not-a-fernet-token' "
                "WHERE provider_name='groq'"
            )
            con.commit()
        broken = status_for("groq")
        assert broken["configured"] is False
        # The stale hint must go too: it invites trust in an unusable key.
        assert broken["key_hint"] is None

        # (b) a valid token that decrypts to nothing
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE provider_keys SET encrypted_key=? WHERE provider_name='groq'",
                (_fernet().encrypt(b"").decode(),),
            )
            con.commit()
        assert status_for("groq")["configured"] is False
