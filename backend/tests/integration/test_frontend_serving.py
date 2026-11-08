"""Single-URL local serving: the built dashboard from the API process.

Contract under test:

* the SPA is mounted only when ``frontend/dist/index.html`` exists,
* API routes win over the catch-all — the mount happens after them,
* unknown browser navigations render ``index.html`` (client-side routing),
* unknown API paths stay JSON 404s,
* ``dist`` files (assets, favicon) are served with sane caching,
* with no build, ``/`` explains how to build it instead of pretending.
"""

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app import frontend as frontend_module
from app.config import settings
from app.frontend import frontend_dist_dir, mount_frontend


@pytest.fixture
def dist(tmp_path) -> Path:
    """A minimal, realistic Vite build output."""
    directory = tmp_path / "dist"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text(
        "<!doctype html><html><body><div id='root'></div>"
        "<script src='/assets/index-abc123.js'></script></body></html>",
        encoding="utf-8",
    )
    (directory / "assets" / "index-abc123.js").write_text(
        "console.log('app')", encoding="utf-8"
    )
    (directory / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return directory


def _api_app(dist_dir=None) -> FastAPI:
    """API + SPA, wired in the real order (API first, dashboard last)."""
    test_app = FastAPI()

    @test_app.get("/api/v1/models")
    async def models():
        return {"models": []}

    @test_app.get("/health")
    async def health():
        return {"status": "ok"}

    mount_frontend(test_app, dist_dir=dist_dir)
    return test_app


async def _get(test_app: FastAPI, path: str, **kwargs):
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path, **kwargs)


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


def test_dist_dir_is_none_without_a_build(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_DIST_DIR", str(tmp_path / "missing"))
    monkeypatch.setattr(frontend_module, "_REPO_ROOT", tmp_path)
    assert frontend_dist_dir() is None


def test_empty_dist_directory_does_not_count(tmp_path, monkeypatch):
    """An interrupted build leaves dist/ behind without index.html."""
    (tmp_path / "frontend" / "dist").mkdir(parents=True)
    monkeypatch.setattr(settings, "FRONTEND_DIST_DIR", "")
    monkeypatch.setattr(frontend_module, "_REPO_ROOT", tmp_path)
    assert frontend_dist_dir() is None


def test_explicit_override_wins(dist, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "FRONTEND_DIST_DIR", str(dist))
    monkeypatch.setattr(frontend_module, "_REPO_ROOT", tmp_path / "nowhere")
    assert frontend_dist_dir() == dist


def test_repo_dist_is_the_default(tmp_path, monkeypatch):
    """<repo>/frontend/dist is found with no configuration at all."""
    repo = tmp_path / "repo"
    built = repo / "frontend" / "dist"
    built.mkdir(parents=True)
    (built / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(settings, "FRONTEND_DIST_DIR", "")
    monkeypatch.setattr(frontend_module, "_REPO_ROOT", repo)
    assert frontend_dist_dir() == built


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


async def test_root_serves_index_html(dist):
    response = await _get(_api_app(dist), "/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<div id='root'>" in response.text
    # The shell must never be cached: it points at hashed asset names.
    assert response.headers["cache-control"] == "no-cache"


async def test_api_routes_are_not_swallowed_by_the_spa(dist):
    response = await _get(_api_app(dist), "/api/v1/models")
    assert response.status_code == 200
    assert response.json() == {"models": []}
    assert (await _get(_api_app(dist), "/health")).json() == {"status": "ok"}


async def test_hashed_assets_are_served_with_immutable_caching(dist):
    response = await _get(_api_app(dist), "/assets/index-abc123.js")
    assert response.status_code == 200
    assert response.text == "console.log('app')"
    assert "immutable" in response.headers["cache-control"]


async def test_static_files_at_the_root_are_served(dist):
    response = await _get(_api_app(dist), "/favicon.svg")
    assert response.status_code == 200
    assert response.text == "<svg/>"


async def test_client_side_routes_render_the_app(dist):
    """A refresh on /providers must not 404."""
    response = await _get(
        _api_app(dist), "/providers", headers={"accept": "text/html"}
    )
    assert response.status_code == 200
    assert "<div id='root'>" in response.text
    assert response.headers["x-routellm-frontend"] == "fallback"


async def test_unknown_api_path_without_html_accept_stays_json(dist):
    """curl or a fetch() typo must not receive an HTML page."""
    response = await _get(
        _api_app(dist), "/api/v1/typo", headers={"accept": "application/json"}
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


async def test_path_traversal_is_not_served(dist):
    response = await _get(
        _api_app(dist), "/../backend/app/config.py", headers={"accept": "*/*"}
    )
    # Starlette normalises the path; the SPA shell or a 404 are both fine, the
    # source file content is not.
    assert "DATABASE_URL" not in response.text


# --------------------------------------------------------------------------
# Mounting rules
# --------------------------------------------------------------------------


def test_mount_is_idempotent(dist):
    test_app = _api_app(dist)
    assert frontend_module.frontend_status(test_app)["mounted"] is True
    assert mount_frontend(test_app) == dist  # second call: no duplicate routes
    paths = [getattr(r, "path", None) for r in test_app.routes]
    assert paths.count("/") == 1


def test_serving_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SERVE_FRONTEND", False)
    test_app = FastAPI()
    assert mount_frontend(test_app, dist_dir=tmp_path) is None
    assert frontend_module.frontend_status(test_app) == {
        "mounted": False,
        "reason": "serving_disabled",
        "directory": None,
    }


def test_missing_build_is_reported_not_faked(monkeypatch):
    """Hermetic: the real repo may well have a dist/ built right now."""
    monkeypatch.setattr(frontend_module, "frontend_dist_dir", lambda: None)
    test_app = FastAPI()
    assert mount_frontend(test_app, dist_dir=None) is None
    status = frontend_module.frontend_status(test_app)
    assert status["mounted"] is False
    assert status["reason"] == "not_built"


async def test_no_build_gets_a_status_page_instead_of_a_bare_404(monkeypatch, tmp_path):
    from app.frontend import install_root_status_page

    test_app = FastAPI()
    install_root_status_page(test_app)
    response = await _get(test_app, "/")
    assert response.status_code == 200
    assert "RouteLLM API is running" in response.text
    assert "npm run build" in response.text
    assert "/docs" in response.text


async def test_status_page_replaces_any_existing_root_route():
    from app.frontend import install_root_status_page

    test_app = FastAPI()

    @test_app.get("/")
    async def old_root():
        return {"old": True}

    install_root_status_page(test_app)
    response = await _get(test_app, "/")
    assert "RouteLLM API is running" in response.text
