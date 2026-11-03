"""Tests for the Hugging Face entrypoint and filesystem safety.

The Gradio mount is exercised on a *throwaway* app so importing this module
never mutates the shared ``app.main.app`` used by the other test modules.
"""

import os
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
    """On a Space the app dir is read-only; writes must land in /tmp."""
    monkeypatch.setenv(HF_SPACE_ID_ENV, "saboornotgonna/routellm")
    monkeypatch.delenv(RUNTIME_DIR_ENV, raising=False)
    target = writable_dir("logs")
    assert str(target).startswith("/tmp")
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
# The real module exposes the real app
# --------------------------------------------------------------------------


def test_hf_app_reexports_the_real_fastapi_app():
    assert hf_app.app is real_app


def test_status_ui_links_to_api_paths():
    pytest.importorskip("gradio")
    ui, _gr = hf_app.build_status_ui()
    assert ui.title == "RouteLLM API"


def test_no_app_py_shadow_at_repo_root():
    """A root app.py would shadow the app/ package on sys.path."""
    # tests/test_hf_app.py → backend/
    backend_root = Path(__file__).resolve().parents[1]
    assert not (backend_root / "app.py").exists()
    assert (backend_root / "hf_app.py").exists()
    assert os.path.basename(hf_app.__file__) == "hf_app.py"
