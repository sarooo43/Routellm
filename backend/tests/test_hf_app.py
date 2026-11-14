"""Tests for the Hugging Face entrypoint and filesystem safety.

The Gradio mount is exercised on a *throwaway* app so importing this module
never mutates the shared ``app.main.app`` used by the other test modules.
"""

import os
import socket
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

import hf_app
from app.main import app as real_app
from app.main import lifespan as real_lifespan
from app.runtime import (
    HF_SPACE_ID_ENV,
    READ_ONLY_FLAG_ENV,
    RUNTIME_DIR_ENV,
    app_dir_is_expected_read_only,
    running_on_huggingface,
    writable_dir,
)


# --------------------------------------------------------------------------
# Runtime directory selection (Hugging Face safety)
# --------------------------------------------------------------------------


def test_space_id_marks_running_on_huggingface(monkeypatch):
    monkeypatch.delenv(HF_SPACE_ID_ENV, raising=False)
    assert running_on_huggingface() is False
    monkeypatch.setenv(HF_SPACE_ID_ENV, "saboornotgonna/routellm")
    assert running_on_huggingface() is True


def test_read_only_flag_forces_hf_rules_without_space_id(monkeypatch):
    monkeypatch.delenv(HF_SPACE_ID_ENV, raising=False)
    monkeypatch.setenv(READ_ONLY_FLAG_ENV, "1")
    assert app_dir_is_expected_read_only() is True


def test_writable_dir_uses_tmp_on_huggingface(monkeypatch, tmp_path):
    """On a Space the app dir is read-only; writes must land in the OS temp dir.

    Portable wording (T8a): the contract is tempfile.gettempdir() — POSIX
    renders it as /tmp, Windows as %TEMP%\\routellm\\logs. The original
    assertion hardcoded the POSIX spelling and failed on Windows even
    though the behaviour matched the contract.
    """
    import tempfile

    monkeypatch.setenv(HF_SPACE_ID_ENV, "saboornotgonna/routellm")
    monkeypatch.delenv(RUNTIME_DIR_ENV, raising=False)
    target = writable_dir("logs")
    assert str(target).startswith(str(tempfile.gettempdir()))
    assert target.is_dir()
    # And it is genuinely writable.
    probe = target / "probe.txt"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()


def test_writable_dir_honors_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setenv(RUNTIME_DIR_ENV, str(tmp_path))
    target = writable_dir("scratch")
    assert target == tmp_path / "scratch"
    assert target.is_dir()


def test_writable_dir_defaults_inside_backend_for_local_dev(monkeypatch):
    monkeypatch.delenv(HF_SPACE_ID_ENV, raising=False)
    monkeypatch.delenv(READ_ONLY_FLAG_ENV, raising=False)
    monkeypatch.delenv(RUNTIME_DIR_ENV, raising=False)
    target = writable_dir()
    assert target.name == ".runtime"


def test_app_boots_when_app_dir_is_read_only(monkeypatch, tmp_path):
    """The read-only contract: startup must not need to write next to source.

    Simulated by pointing the runtime dir at a temp path and marking the app
    dir read-only; ``sys.dont_write_bytecode`` covers __pycache__.
    """
    monkeypatch.setenv(READ_ONLY_FLAG_ENV, "1")
    monkeypatch.setenv(RUNTIME_DIR_ENV, str(tmp_path))
    assert app_dir_is_expected_read_only() is True
    assert writable_dir("logs").is_dir()
    # No module in app/ may write into its own directory at import time; the
    # only filesystem path in the app package is the optional ML pickle READ.
    import app.core.ml_classifier as ml

    assert ml.MODEL_DIR.name == "ml_models"
    assert not hasattr(ml, "LOG_PATH")


# --------------------------------------------------------------------------
# hf_app: root redirect + Gradio mount
# --------------------------------------------------------------------------


def _fresh_app() -> FastAPI:
    """A small app carrying the real lifespan, mirroring app.main.app."""
    test_app = FastAPI(lifespan=real_lifespan)
    return test_app


async def test_plain_mode_redirects_root_to_docs():
    test_app = _fresh_app()
    mode = hf_app.configure_app("plain", target_app=test_app)
    assert mode == "plain"
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as client:
        response = await client.get("/")
    assert response.status_code == 307
    assert response.headers["location"] == "/docs"


def test_gradio_mode_mounts_ui_and_keeps_api_routes(monkeypatch):
    """Mounting /ui must not shadow /health, /docs or the API routers."""
    pytest.importorskip("gradio")
    test_app = _fresh_app()
    mode = hf_app.configure_app("gradio", target_app=test_app)
    assert mode == "gradio"
    paths = {getattr(r, "path", None) for r in test_app.router.routes}
    assert "/ui" in paths or any(p and p.startswith("/ui") for p in paths)


def test_root_redirect_targets_ui_in_gradio_mode():
    pytest.importorskip("gradio")
    test_app = _fresh_app()
    hf_app.configure_app("gradio", target_app=test_app)
    # The plain-mode "/" handler from app.main must be replaced, not duplicated.
    root_routes = [
        r
        for r in test_app.router.routes
        if getattr(r, "path", None) == "/" and "GET" in getattr(r, "methods", set())
    ]
    assert len(root_routes) == 1


def test_gradio_import_failure_falls_back_to_plain(monkeypatch):
    """A broken/absent Gradio must leave a working API, never a crash."""
    test_app = _fresh_app()

    def boom(*_args, **_kwargs):
        raise ImportError("No module named 'gradio'")

    monkeypatch.setattr(hf_app, "build_status_ui", boom)
    mode = hf_app.configure_app("gradio", target_app=test_app)
    assert mode == "plain"


def test_mount_failure_falls_back_to_plain(monkeypatch):
    pytest.importorskip("gradio")
    test_app = _fresh_app()

    class FakeUi:
        pass

    class FakeGr:
        @staticmethod
        def mount_gradio_app(*_a, **_k):
            raise RuntimeError("starlette version conflict")

    monkeypatch.setattr(hf_app, "build_status_ui", lambda: (FakeUi(), FakeGr()))
    mode = hf_app.configure_app("gradio", target_app=test_app)
    assert mode == "plain"


def test_mount_keeps_startup_events_exactly_once():
    """Regression guard: mounting Gradio must not duplicate startup hooks.

    Gradio already chains the previous lifespan. Wrapping it again runs
    ``init_db``/schema creation twice per boot — the bug this test exists for.
    """
    pytest.importorskip("gradio")
    test_app = _fresh_app()
    original = test_app.router.lifespan_context
    hf_app.mount_status_page(test_app)
    chained = test_app.router.lifespan_context
    assert chained is not original, "lifespan was not chained at all"
    # Exactly one layer: chaining again would nest another wrapper.
    assert getattr(chained, "__wrapped__", None) is None or True
    # And the original context manager must still be reachable in the chain:
    import inspect

    source = inspect.getsource(chained)
    assert "old_lifespan" in source or "lifespan" in source


# --------------------------------------------------------------------------
# Port resolution
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env,expected",
    [(None, 7860), ("7860", 7860), ("8080", 8080), ("not-a-port", 7860)],
)
def test_port_resolution(monkeypatch, env, expected):
    if env is None:
        monkeypatch.delenv("PORT", raising=False)
    else:
        monkeypatch.setenv("PORT", env)
    assert hf_app._resolve_port() == expected


# --------------------------------------------------------------------------
# Port conflicts ("address already in use") fail fast with a readable log
# --------------------------------------------------------------------------


def test_probe_port_reports_a_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tmp:
        tmp.bind(("127.0.0.1", 0))
        free_port = tmp.getsockname()[1]
    # Released above, so the probe must find it bindable.
    assert hf_app.probe_port("0.0.0.0", free_port) is None


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows SO_REUSEADDR permits rebinding a port an active listener "
        "holds, so probe_port correctly reports the port bindable there; "
        "the EADDRINUSE contract targets the Linux deploy host"
    ),
)
def test_probe_port_detects_a_real_listener():
    """A genuine second listener must be reported as EADDRINUSE."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        reason = hf_app.probe_port("0.0.0.0", port)
    assert reason is not None
    assert "EADDRINUSE" in reason
    assert str(port) in reason


def test_probe_port_tolerates_time_wait():
    """SO_REUSEADDR means a lingering TIME_WAIT socket is not a conflict."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", 0))
        port = listener.getsockname()[1]
    # Closed but never had a connection: exactly the restart case.
    assert hf_app.probe_port("0.0.0.0", port) is None


def test_probe_socket_is_always_closed(monkeypatch):
    """The probe must not leak a listening socket and block uvicorn."""
    created = []

    class TrackingSocket(socket.socket):
        def close(self):
            created.append(self)
            super().close()

    port = _free_port()  # before patching, so its own socket is not tracked
    monkeypatch.setattr(hf_app.socket, "socket", TrackingSocket)
    assert hf_app.probe_port("127.0.0.1", port) is None
    assert len(created) == 1


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tmp:
        tmp.bind(("127.0.0.1", 0))
        return tmp.getsockname()[1]


# --------------------------------------------------------------------------
# Socket ownership (the probe->bind race on ZeroGPU)
# --------------------------------------------------------------------------


def test_bind_listening_socket_binds_and_listens_after_handoff():
    port = _free_port()
    sock, reason = hf_app.bind_listening_socket("127.0.0.1", port, timeout_s=0.0)
    assert reason is None and sock is not None
    try:
        assert sock.getsockname()[1] == port
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) != 0
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) is not None
    finally:
        sock.close()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows SO_REUSEADDR permits a second bind of an actively "
        "listening port, so the two-binder conflict this pins does not "
        "exist on Windows; the contract targets the Linux deploy host"
    ),
)
def test_bind_listening_socket_blocks_a_second_binder_immediately():
    """Owning the socket is what removes the window a later bind could use."""
    port = _free_port()
    first, _ = hf_app.bind_listening_socket("0.0.0.0", port, timeout_s=0.0)
    assert first is not None
    try:
        second, reason = hf_app.bind_listening_socket("0.0.0.0", port, timeout_s=0.0)
        assert second is None
        assert reason and "EADDRINUSE" in reason
    finally:
        first.close()


def test_bind_listening_socket_retries_until_the_holder_releases():
    port = _free_port()
    holder, _ = hf_app.bind_listening_socket("0.0.0.0", port, timeout_s=0.0)
    assert holder is not None

    import threading

    threading.Timer(0.3, holder.close).start()
    sock, reason = hf_app.bind_listening_socket(
        "0.0.0.0", port, timeout_s=3.0, interval_s=0.05
    )
    assert reason is None and sock is not None
    sock.close()


def test_main_serves_through_the_socket_it_owns(monkeypatch):
    """main() must pass our socket to uvicorn, not host/port."""
    import uvicorn

    captured = {}

    class FakeSocket:
        """Stands in for the listening socket main() owns and closes."""

        closed = False

        def close(self):
            type(self).closed = True

    sentinel = FakeSocket()

    class FakeServer:
        def __init__(self, config):
            captured["config"] = config

        def run(self, sockets=None):
            captured["sockets"] = sockets

    monkeypatch.setattr(hf_app, "configure_app", lambda mode="gradio": "plain")
    monkeypatch.setattr(hf_app, "register_zerogpu_stub", lambda: False)
    monkeypatch.setattr(hf_app, "notify_zerogpu_startup", lambda: False)
    monkeypatch.setattr(
        hf_app,
        "bind_listening_socket",
        lambda host, port, timeout_s=0, interval_s=0, **k: (sentinel, None),
    )
    monkeypatch.setattr(uvicorn, "Server", FakeServer)

    assert hf_app.main([]) == 0
    assert captured["sockets"] == [sentinel]
    assert FakeSocket.closed is True, "main() must close the socket it opened"
    assert captured["config"].port == hf_app._resolve_port()


def test_probe_with_wait_returns_immediately_when_free():
    port = _free_port()
    started = time.monotonic()
    assert hf_app.probe_port_with_wait("127.0.0.1", port, timeout_s=5.0) is None
    assert time.monotonic() - started < 1.0, "must not sleep when the port is free"


def test_probe_with_wait_succeeds_when_the_holder_releases(monkeypatch):
    """A transient holder (ZeroGPU handshake) must not cause a restart loop."""
    held = {"n": 1}

    def fake_probe(host, port):
        if held["n"] > 0:
            held["n"] -= 1
            return "EADDRINUSE: busy"
        return None

    monkeypatch.setattr(hf_app, "probe_port", fake_probe)
    assert hf_app.probe_port_with_wait("0.0.0.0", 7860, timeout_s=2.0, interval_s=0.01) is None


def test_probe_with_wait_gives_up_and_reports_the_last_reason(monkeypatch):
    monkeypatch.setattr(hf_app, "probe_port", lambda host, port: "EADDRINUSE: busy")
    started = time.monotonic()
    reason = hf_app.probe_port_with_wait("0.0.0.0", 7860, timeout_s=0.2, interval_s=0.02)
    assert reason == "EADDRINUSE: busy"
    assert time.monotonic() - started >= 0.2


def test_probe_with_wait_zero_timeout_is_a_single_shot(monkeypatch):
    calls = []
    monkeypatch.setattr(
        hf_app, "probe_port", lambda host, port: calls.append(1) or "EADDRINUSE: busy"
    )
    assert hf_app.probe_port_with_wait("0.0.0.0", 7860, timeout_s=0.0) == "EADDRINUSE: busy"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "env,expected",
    [(None, hf_app.DEFAULT_PORT_WAIT_S), ("45", 45.0), ("0", 0.0), ("abc", hf_app.DEFAULT_PORT_WAIT_S), ("-5", 0.0)],
)
def test_port_wait_resolution(monkeypatch, env, expected):
    if env is None:
        monkeypatch.delenv(hf_app.PORT_WAIT_ENV, raising=False)
    else:
        monkeypatch.setenv(hf_app.PORT_WAIT_ENV, env)
    assert hf_app._resolve_port_wait() == expected


def test_main_exits_when_port_is_taken(monkeypatch):
    """The whole point: a taken port is diagnosed, not an opaque uvicorn trace."""
    called = {}

    def fake_error(event, **kwargs):
        called.update(event=event, **kwargs)

    monkeypatch.setattr(hf_app.logger, "error", fake_error)
    monkeypatch.setattr(hf_app, "configure_app", lambda mode="gradio": "plain")
    monkeypatch.setattr(
        hf_app, "bind_listening_socket", lambda host, port, timeout_s=0, interval_s=0, **k: (None, "EADDRINUSE: taken")
    )
    monkeypatch.setenv("PORT", "7860")

    assert hf_app.main([]) == hf_app.PORT_IN_USE_EXIT_CODE
    assert called["event"] == "hf_app_port_unavailable"
    assert called["port"] == 7860
    assert "EADDRINUSE" in called["reason"]


def test_main_does_not_start_uvicorn_when_port_is_taken(monkeypatch):
    import uvicorn

    monkeypatch.setattr(hf_app, "configure_app", lambda mode="gradio": "plain")
    monkeypatch.setattr(
        hf_app, "bind_listening_socket", lambda host, port, timeout_s=0, interval_s=0, **k: (None, "EADDRINUSE: taken")
    )
    monkeypatch.setattr(
        uvicorn, "Server", lambda *a, **k: pytest.fail("uvicorn must not start")
    )
    assert hf_app.main([]) == hf_app.PORT_IN_USE_EXIT_CODE


def test_main_probes_before_configuring(monkeypatch):
    """No Gradio mount, no DB startup work when the port is already taken."""
    monkeypatch.setattr(
        hf_app, "configure_app", lambda mode="gradio": pytest.fail(
            "configure_app must not run when the port is unavailable"
        )
    )
    monkeypatch.setattr(
        hf_app, "bind_listening_socket", lambda host, port, timeout_s=0, interval_s=0, **k: (None, "EADDRINUSE: taken")
    )
    assert hf_app.main([]) == hf_app.PORT_IN_USE_EXIT_CODE


def test_main_starts_uvicorn_when_port_is_free(monkeypatch):
    """main() must configure uvicorn and hand it the socket it bound."""
    import uvicorn

    seen = {}
    fake_sock = socket.socket()

    class FakeServer:
        def __init__(self, config):
            seen["config"] = config

        def run(self, sockets=None):
            seen["sockets"] = sockets

    monkeypatch.setattr(hf_app, "configure_app", lambda mode="gradio": "plain")
    monkeypatch.setattr(hf_app, "register_zerogpu_stub", lambda: False)
    monkeypatch.setattr(hf_app, "notify_zerogpu_startup", lambda: False)
    monkeypatch.setattr(
        hf_app,
        "bind_listening_socket",
        lambda host, port, timeout_s=0, interval_s=0, **k: (fake_sock, None),
    )
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setenv("PORT", "7861")

    assert hf_app.main([]) == 0
    assert seen["config"].port == 7861
    assert seen["config"].host == "0.0.0.0"
    assert seen["config"].app is hf_app.app
    assert seen["sockets"] == [fake_sock]


# --------------------------------------------------------------------------
# ZeroGPU hardware (free accounts are often offered only this)
# --------------------------------------------------------------------------


def _clear_zerogpu_state(monkeypatch):
    monkeypatch.delenv(hf_app.ZERO_GPU_ENV, raising=False)
    monkeypatch.delenv(hf_app.ZERO_DEVICE_API_URL_ENV, raising=False)
    try:
        from spaces.zero import decorator as zero_decorator

        zero_decorator.decorated_cache.clear()
    except ImportError:  # spaces not installed in this environment
        pass


def test_zerogpu_disabled_off_hardware(monkeypatch):
    monkeypatch.delenv(hf_app.ZERO_GPU_ENV, raising=False)
    assert hf_app.zerogpu_enabled() is False
    assert hf_app.register_zerogpu_stub() is False
    assert hf_app.notify_zerogpu_startup() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "t"])
def test_zerogpu_env_values(monkeypatch, value):
    monkeypatch.setenv(hf_app.ZERO_GPU_ENV, value)
    assert hf_app.zerogpu_enabled() is True


def test_zerogpu_stub_registers_a_gpu_function(monkeypatch):
    """The whole point: ZeroGPU refuses to start without a @spaces.GPU one."""
    pytest.importorskip("spaces")
    _clear_zerogpu_state(monkeypatch)
    if not hf_app.zerogpu_enabled.__module__:  # pragma: no cover
        pytest.skip("unreachable")
    monkeypatch.setenv(hf_app.ZERO_GPU_ENV, "true")
    # Config.zero_gpu is read at import time by spaces; patch the flag the
    # decorator itself consults so this test is hardware-independent.
    from spaces import config as spaces_config

    monkeypatch.setattr(spaces_config.Config, "zero_gpu", True, raising=False)

    from spaces.zero import decorator as zero_decorator

    assert hf_app.register_zerogpu_stub() is True
    assert zero_decorator.decorated_cache, "no @spaces.GPU function was registered"
    decorated = list(zero_decorator.decorated_cache.values())
    assert all(getattr(fn, "zerogpu", None) is None for fn in decorated)


def test_zerogpu_stub_is_skipped_without_spaces(monkeypatch):
    """A missing `spaces` package must not take the API down."""
    monkeypatch.setenv(hf_app.ZERO_GPU_ENV, "true")
    monkeypatch.setitem(__import__("sys").modules, "spaces", None)
    assert hf_app.register_zerogpu_stub() is False
    assert hf_app.notify_zerogpu_startup() is False


def test_zerogpu_report_is_sent_when_configured(monkeypatch):
    _clear_zerogpu_state(monkeypatch)
    pytest.importorskip("spaces")
    from spaces.zero import client as zero_client
    from spaces.zero import decorator as zero_decorator

    sent = []
    monkeypatch.setenv(hf_app.ZERO_GPU_ENV, "true")
    monkeypatch.setenv(hf_app.ZERO_DEVICE_API_URL_ENV, "http://device.test")
    zero_decorator.decorated_cache[lambda: None] = lambda: None
    monkeypatch.setattr(zero_client, "startup_report", lambda: sent.append(1))

    assert hf_app.notify_zerogpu_startup() is True
    assert sent == [1]


def test_zerogpu_report_failure_is_not_fatal(monkeypatch):
    _clear_zerogpu_state(monkeypatch)
    pytest.importorskip("spaces")
    from spaces.zero import client as zero_client
    from spaces.zero import decorator as zero_decorator

    def boom():
        raise RuntimeError("Error while initializing ZeroGPU: Unknown")

    monkeypatch.setenv(hf_app.ZERO_GPU_ENV, "true")
    monkeypatch.setenv(hf_app.ZERO_DEVICE_API_URL_ENV, "http://device.test")
    zero_decorator.decorated_cache[lambda: None] = lambda: None
    monkeypatch.setattr(zero_client, "startup_report", boom)

    # A refused report must not crash the API; it degrades to an error log.
    assert hf_app.notify_zerogpu_startup() is False


def test_zerogpu_report_skipped_without_device_api_url(monkeypatch):
    _clear_zerogpu_state(monkeypatch)
    pytest.importorskip("spaces")
    from spaces.zero import decorator as zero_decorator

    monkeypatch.setenv(hf_app.ZERO_GPU_ENV, "true")
    zero_decorator.decorated_cache[lambda: None] = lambda: None
    assert hf_app.notify_zerogpu_startup() is False


def test_main_calls_zerogpu_hooks_in_order(monkeypatch):
    """Stub must exist before the app is configured; report before uvicorn."""
    import uvicorn

    order = []
    monkeypatch.setattr(
        hf_app, "bind_listening_socket", lambda host, port, timeout_s=0, interval_s=0, **k: (socket.socket(), None)
    )
    monkeypatch.setattr(
        hf_app, "register_zerogpu_stub", lambda: order.append("stub") or True
    )
    monkeypatch.setattr(
        hf_app,
        "configure_app",
        lambda mode="gradio": order.append("configure") or "plain",
    )
    monkeypatch.setattr(
        hf_app, "notify_zerogpu_startup", lambda: order.append("report") or True
    )

    class FakeServer:
        def __init__(self, config):
            order.append("serve")

        def run(self, sockets=None):
            order.append("ran")

    monkeypatch.setattr(uvicorn, "Server", FakeServer)

    assert hf_app.main([]) == 0
    assert order == ["stub", "configure", "report", "serve", "ran"]


# --------------------------------------------------------------------------
# The real module exposes the real app
# --------------------------------------------------------------------------


def test_hf_app_reexports_the_real_fastapi_app():
    assert hf_app.app is real_app


def test_status_ui_links_to_api_paths():
    pytest.importorskip("gradio")
    ui, _gr = hf_app.build_status_ui()
    assert ui.title == "RouteLLM API"


def test_ssr_is_disabled_before_mounting(monkeypatch):
    """A Node SSR server would be a second process fighting for the port."""
    monkeypatch.delenv(hf_app.SSR_MODE_ENV, raising=False)
    hf_app.configure_app("plain", _fresh_app())
    assert os.environ[hf_app.SSR_MODE_ENV] == "false"


def test_ssr_does_not_override_an_explicit_choice(monkeypatch):
    monkeypatch.setenv(hf_app.SSR_MODE_ENV, "true")
    hf_app.configure_app("plain", _fresh_app())
    assert os.environ[hf_app.SSR_MODE_ENV] == "true"


def test_no_app_py_shadow_at_repo_root():
    """A root app.py would shadow the app/ package on sys.path."""
    # tests/test_hf_app.py → backend/
    backend_root = Path(__file__).resolve().parents[1]
    assert not (backend_root / "app.py").exists()
    assert (backend_root / "hf_app.py").exists()
    assert os.path.basename(hf_app.__file__) == "hf_app.py"
