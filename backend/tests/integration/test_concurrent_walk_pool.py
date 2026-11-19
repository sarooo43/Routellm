"""Phase 2h: concurrency must not turn a retired model into a 503 storm.

OBSERVED on a real uvicorn server + real SQLite before the fix: 30 concurrent
requests against a provider answering 404 took 91 s and answered

    15 x HTTP 503 {"detail": "Database temporarily unavailable"}
    15 x HTTP 200 (model_not_found)

because every request that hit a retired model raced to delete the discovery
cache row on its own session while holding a pool connection, in SQLite's
default rollback-journal mode where a writer blocks every reader. After the
fix: 30/30 succeed in ~1.5 s with ONE discovery call.

This test drives the real app, not the executor, because the failure was in
the interaction between the walk, the cache write and the connection pool.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Retired(BaseHTTPRequestHandler):
    """Every chat call says the model is gone; the list call works."""

    calls = {"list": 0, "chat": 0}
    delay = 0.2

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).calls["list"] += 1
        time.sleep(type(self).delay)
        self._send(200, {"data": [{"id": "llama-3.1-8b-instant"}]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        type(self).calls["chat"] += 1
        time.sleep(type(self).delay)
        self._send(404, {"error": {"message": "model not found"}})

    def log_message(self, *args):
        pass


@pytest.fixture
def server(tmp_path, monkeypatch):
    _Retired.calls = {"list": 0, "chat": 0}
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        up = probe.getsockname()[1]
    stub = ThreadingHTTPServer(("127.0.0.1", up), _Retired)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    port = _free_port()
    env = dict(os.environ)
    env.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'c.db'}",
            "REDIS_URL": "",
            "DEMO_MODE_ENABLED": "false",
            "CHAOS_MODE_ENABLED": "false",
            "REQUIRE_API_KEY": "false",
            "ALLOWED_HOSTS": "*",
            # The default 30/minute would answer 429 long before the pool
            # matters; this test is about the database, not the limiter.
            "ROUTE_RATE_LIMIT": "100000/minute",
            "GROQ_BASE_URL": f"http://127.0.0.1:{up}/v1",
            "PYTHONPATH": str(REPO_ROOT / "backend"),
        }
    )
    # Conftest-level import for the spec override: the app must point at the
    # stub, so a tiny sitecustomize-style preamble is used instead.
    env["PYTHONSTARTUP"] = ""

    preamble = (
        "import sys, runpy;"
        "from dataclasses import replace;"
        "import app.providers.model_registry as mr;"
        f"mr.PROVIDER_SPECS['groq'] = replace(mr.PROVIDER_SPECS['groq'], base_url='http://127.0.0.1:{up}/v1');"
        "import uvicorn; uvicorn.run('app.main:app', host='127.0.0.1', "
        f"port={port}, log_level='critical')"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", preamble],
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
        yield {"base": base, "stub": stub, "db": tmp_path / "c.db"}
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            proc.communicate()
        stub.shutdown()
        stub.server_close()


def test_concurrent_retired_models_do_not_exhaust_the_pool(server):
    async def run():
        async with httpx.AsyncClient(base_url=server["base"], timeout=300.0) as c:
            key = (
                await c.post("/api/v1/keys/generate", json={"label": "c"})
            ).json()["api_key"]
            headers = {"X-API-Key": key}
            await c.put(
                "/api/v1/provider-keys/groq",
                json={"api_key": "gsk_FAKE0000000000000000"},
                headers=headers,
            )
            # Warm discovery so the burst measures the walk, not the first list.
            await c.post(
                "/api/v1/route",
                json={"prompt": "warm", "force_provider": "groq", "max_fallbacks": 0},
                headers=headers,
            )
            _Retired.calls = {"list": 0, "chat": 0}

            async def one(i: int) -> int:
                r = await c.post(
                    "/api/v1/route",
                    json={"prompt": f"x{i}", "force_provider": "groq",
                          "max_fallbacks": 0},
                    headers=headers,
                )
                return r.status_code

            started = time.monotonic()
            codes = await asyncio.gather(*[one(i) for i in range(30)])
            return list(codes), time.monotonic() - started

    codes, elapsed = asyncio.run(run())

    # Before the fix: 15 x 503 + 15 x 200 in 91 s.
    assert 503 not in codes, (
        f"pool exhaustion is back: {codes.count(503)}/30 answered "
        "503 Database temporarily unavailable"
    )
    assert all(code == 200 for code in codes), codes
    assert elapsed < 30, f"30 concurrent requests took {elapsed:.1f}s"
    # One discovery call, not one per request.
    assert _Retired.calls["list"] <= 3, _Retired.calls
    assert _Retired.calls["chat"] == 30, _Retired.calls
