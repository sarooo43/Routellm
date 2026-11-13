"""Deployment-readiness contracts: health, fail-soft, Redis TLS, boot safety.

These cover the situations a managed deployment (Hugging Face Space + Neon +
Upstash) actually hits: a sleeping database, an unreachable Redis, a
brand-new empty schema and a container whose app directory is read-only.
"""

import asyncio
import sys

import httpx
import pytest
from fakeredis import FakeAsyncRedis

from app.api import routes as routes_module
from app.cache import ping_redis
from app.config import normalize_redis_url, redis_uses_tls, settings
from app.main import app
from app.providers.registry import PROVIDER_REGISTRY, routable_providers


@pytest.fixture
async def client():
    """Bare client — no DB override, so the app's own wiring is exercised."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# --------------------------------------------------------------------------
# /health contract
# --------------------------------------------------------------------------


async def test_health_is_public_and_reports_components(client, monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", None)
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in {"ok", "degraded"}
    assert body["service"] == "routellm-backend"
    assert body["version"]
    assert body["uptime_s"] >= 0
    # Flat aliases the deploy smoke test relies on.
    assert body["db"] in {"up", "down", "not_configured", "ok"}
    assert body["redis"] in {"up", "down", "ok"}
    # Existing dashboard contract: the pill reads these keys.
    assert set(body["components"]) == {"database", "redis"}


async def test_health_stays_200_when_every_dependency_is_down(client, monkeypatch):
    """/health must never 503: a 503 is indistinguishable from "dead Space"
    for several clients, and the body already carries the degradation."""
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql://u:p@127.0.0.1:1/db")
    monkeypatch.setattr(settings, "REDIS_URL", "rediss://127.0.0.1:1")

    import app.core.executor as executor_module
    import app.db.session as session_module

    # The shared breaker/quota/chaos trio is cached process-wide; reset it so
    # the unreachable REDIS_URL above is actually the one used.
    monkeypatch.setattr(executor_module, "_circuit_breaker", None)
    monkeypatch.setattr(executor_module, "_quota_tracker", None)
    monkeypatch.setattr(executor_module, "_chaos_controller", None)
    await session_module.reset_engine()
    try:
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["db"] == "down"
        assert body["redis"] == "down"
    finally:
        await session_module.reset_engine()


async def test_health_probe_timeout_is_respected(client, monkeypatch):
    """A hung dependency must not hold /health past its probe budget."""
    monkeypatch.setattr(settings, "HEALTH_PROBE_TIMEOUT_S", 0.05)

    class HangingRedis:
        async def ping(self):
            await asyncio.sleep(5)

    monkeypatch.setattr(
        routes_module, "_get_breaker", lambda: type("B", (), {"redis": HangingRedis()})()
    )
    monkeypatch.setattr(settings, "DATABASE_URL", None)

    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["redis"] == "down"


# --------------------------------------------------------------------------
# Fail-soft reads
# --------------------------------------------------------------------------


async def test_models_degrades_to_static_when_database_is_unreachable(
    client, monkeypatch
):
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql://u:p@127.0.0.1:1/db")
    import app.db.session as session_module

    await session_module.reset_engine()
    try:
        resp = await client.get("/api/v1/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["live_stats_available"] is False
        assert len(body["providers"]) == len(routable_providers())
        for provider in body["providers"]:
            assert provider["success_rate_overall"] is None
            assert provider["requests_today"] is None
            assert provider["daily_quota_limit"] is None
            assert provider["quota_percentage_used"] is None
            assert provider["status"] in {"unknown", "circuit_open"}
    finally:
        await session_module.reset_engine()


async def test_catalog_degrades_to_static_when_database_is_unreachable(
    client, monkeypatch
):
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql://u:p@127.0.0.1:1/db")
    import app.db.session as session_module

    await session_module.reset_engine()
    try:
        resp = await client.get("/api/v1/catalog")
        assert resp.status_code == 200
        body = resp.json()
        assert body["live_stats_available"] is False
        assert len(body["models"]) == len(routable_providers())
        for entry in body["models"]:
            assert entry["live_success_rate"] is None
            assert entry["display_name"]
            assert entry["context_window"] > 0
    finally:
        await session_module.reset_engine()


async def test_protected_routes_still_answer_clean_503_without_a_database(
    client, monkeypatch
):
    """A broken database must surface as a clean 503, never a 500.

    LOCAL-FIRST NOTE: an unset DATABASE_URL now means "use SQLite", so the only
    way to have *no* database at all is a data directory that cannot be created
    (read-only host, bad permissions). That is simulated here by making the
    data-dir resolver raise OSError, which is exactly what the real code path
    does when the directory is unusable.
    """
    import app.local as local_module

    def _unwritable(*_args, **_kwargs):
        raise OSError("data directory is not writable (simulated)")

    monkeypatch.setattr(local_module, "data_dir", _unwritable)
    monkeypatch.setattr(settings, "DATABASE_URL", None)
    resp = await client.post("/api/v1/route", json={"prompt": "hi"})
    assert resp.status_code == 503, resp.text
    assert resp.json() == {"detail": "Database temporarily unavailable"}


async def test_real_refused_tcp_connect_answers_503_not_500(client, monkeypatch):
    """The Postgres-down case, for real: a DSN pointing at a CLOSED port.

    SQLAlchemy 2.1 + asyncpg leaks the builtin ConnectionRefusedError here —
    it is an OSError, NOT a SQLAlchemyError — so only the dedicated handler
    keeps this a clean 503. Verified end-to-end against the real engine
    (no mocks): the dependency chain must let the raw error reach the app.
    """
    import socket

    import app.db.session as session_module

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()  # nothing listens here anymore: connects are refused

    await session_module.reset_engine()
    monkeypatch.setattr(
        settings,
        "DATABASE_URL",
        f"postgresql+asyncpg://routellm:pw@127.0.0.1:{closed_port}/routellm",
    )
    try:
        resp = await client.post("/api/v1/route", json={"prompt": "hi"})
        assert resp.status_code == 503, resp.text
        assert resp.json() == {"detail": "Database temporarily unavailable"}
    finally:
        # Hand the SQLite engine back to every other test in the suite.
        await session_module.reset_engine()


# --------------------------------------------------------------------------
# Schema creation
# --------------------------------------------------------------------------


async def test_ensure_schema_is_non_fatal_and_idempotent(monkeypatch):
    """A brand-new DB: create succeeds once, retries are cheap no-ops."""
    import app.db.session as session_module

    calls = {"create_all": 0}

    class FakeConn:
        async def run_sync(self, fn):
            calls["create_all"] += 1

        async def execute(self, statement):
            return None

    class FakeEngine:
        def begin(self):
            conn = FakeConn()

            class _Ctx:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *exc):
                    return False

            return _Ctx()

    await session_module.reset_engine()
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setattr(session_module, "get_engine", lambda: FakeEngine())

    try:
        assert await session_module.ensure_schema() is True
        assert calls["create_all"] == 1
        assert session_module.schema_ready() is True
        # Second call short-circuits on the flag.
        assert await session_module.ensure_schema() is True
        assert calls["create_all"] == 1
    finally:
        await session_module.reset_engine()


async def test_ensure_schema_returns_false_instead_of_raising(monkeypatch):
    import app.db.session as session_module

    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql://u:p@host/db")

    def boom():
        raise session_module.DatabaseUnavailableError("connection refused")

    await session_module.reset_engine()
    monkeypatch.setattr(session_module, "get_engine", boom)
    try:
        assert await session_module.ensure_schema() is False
        assert session_module.schema_ready() is False
    finally:
        await session_module.reset_engine()


async def test_unset_database_url_defaults_to_sqlite(monkeypatch, tmp_path):
    """LOCAL-FIRST: no DATABASE_URL means SQLite in the data dir, not "no DB".

    Previously this asserted ``ensure_schema() is False`` — the app refused to
    serve data without configuration. A fresh clone must now just work, so the
    assertion is inverted: the schema is created against a real SQLite file.
    """
    import app.db.session as session_module

    monkeypatch.setenv("ROUTELLM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DATABASE_URL", None)
    monkeypatch.setattr(settings, "ROUTELLM_DATA_DIR", str(tmp_path))
    await session_module.reset_engine()
    try:
        assert await session_module.ensure_schema() is True
        assert session_module.schema_ready() is True
        assert (tmp_path / "routellm.db").exists(), "SQLite file was not created"
    finally:
        await session_module.reset_engine()


async def test_effective_database_url_reports_the_sqlite_default(monkeypatch, tmp_path):
    import app.db.session as session_module

    monkeypatch.setattr(settings, "DATABASE_URL", None)
    monkeypatch.setattr(settings, "ROUTELLM_DATA_DIR", str(tmp_path))
    url = session_module.effective_database_url()
    assert url is not None and url.startswith("sqlite+aiosqlite:///")
    assert str(tmp_path) in url


# --------------------------------------------------------------------------
# Redis
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_tls",
    [
        ("redis://localhost:6379/0", False),
        ("rediss://default:pw@host.upstash.io:6379", True),
    ],
)
def test_redis_url_forms(raw, expected_tls):
    assert normalize_redis_url(raw) == raw
    assert redis_uses_tls(raw) is expected_tls


def test_redis_url_rejects_non_redis_scheme():
    with pytest.raises(ValueError, match="redis://"):
        normalize_redis_url("http://localhost:6379")


def test_redis_url_unset_returns_none():
    assert normalize_redis_url(None) is None
    assert normalize_redis_url("   ") is None


async def test_ping_redis_reports_ok_for_a_live_client():
    client = FakeAsyncRedis(decode_responses=True)
    try:
        assert await ping_redis(client) == "ok"
    finally:
        await client.aclose()


async def test_ping_redis_reports_down_when_unreachable():
    import redis.asyncio as redis_async

    client = redis_async.from_url(
        "redis://127.0.0.1:1", socket_connect_timeout=0.2, socket_timeout=0.2
    )
    try:
        assert await ping_redis(client, timeout=0.5) == "down"
    finally:
        await client.aclose()


def test_redis_client_uses_tls_scheme_for_rediss(monkeypatch):
    """The client must be built from the DSN as-is (scheme selects TLS)."""
    from app.cache import create_redis_client

    monkeypatch.setattr(settings, "REDIS_URL", "rediss://default:pw@example.upstash.io:6379")
    client = create_redis_client()
    try:
        assert client.connection_pool.connection_class.__name__ == "SSLConnection"
    finally:
        # No I/O happened; closing is still tidy.
        asyncio.get_event_loop_policy()


# --------------------------------------------------------------------------
# Heavy imports stay off the default path
# --------------------------------------------------------------------------


def _windows_socket_provider_broken() -> bool:
    """Runtime condition, not a platform blanket skip (T8a).

    In this Windows sandbox the subprocess's socket provider fails to
    initialize (OSError 10106, WSAStartup) — ANY subprocess that imports
    socket-backed modules dies regardless of what it does. On a normal
    Windows machine and on Linux CI this returns False and the test runs.
    """
    if sys.platform != "win32":
        return False
    import subprocess as _subprocess

    probe = _subprocess.run(
        [sys.executable, "-c", "import socket; socket.socket()"],
        capture_output=True,
        text=True,
    )
    return probe.returncode != 0 and "10106" in probe.stderr


@pytest.mark.skipif(
    _windows_socket_provider_broken(),
    reason=(
        "This sandbox cannot initialize the Windows socket provider in a "
        "subprocess (WinError 10106, WSAStartup); the test spawns a bare "
        "interpreter. Runs normally on Linux CI and healthy Windows hosts"
    ),
)
def test_ml_packages_are_not_imported_when_ml_routing_is_off():
    import os
    import subprocess
    import sys
    from pathlib import Path

    backend_root = Path(__file__).resolve().parents[2]
    code = (
        "import app.main, app.core.executor, app.core.scorer\n"
        "import sys\n"
        "heavy = [m for m in ('torch','sentence_transformers','sklearn','joblib','numpy')"
        " if m in sys.modules]\n"
        "print(','.join(heavy))\n"
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "ENABLE_ML_ROUTING": "false",
        "PYTHONPATH": str(backend_root),
    }
    if sys.platform == "win32":
        # Windows child processes need the OS variables or the socket
        # provider fails to initialize (WinError 10106 in _overlapped) —
        # that failure said nothing about ML imports. Keep a minimal but
        # sufficient Windows env; Linux keeps the stripped POSIX one.
        env["PATH"] = os.environ.get("PATH", "")
        for var in ("SystemRoot", "SystemDrive", "windir", "COMSPEC", "TEMP", "TMP"):
            value = os.environ.get(var)
            if value:
                env[var] = value
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=backend_root,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        f"heavy ML modules imported on the default path: {result.stdout.strip()}"
    )
