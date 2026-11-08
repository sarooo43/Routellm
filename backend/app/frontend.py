"""Serve the built dashboard from the API process (single-URL local runs).

Local-first means one command, one URL: ``python run.py`` starts uvicorn and the
dashboard is already there at ``http://127.0.0.1:8000/``. There is no second
server to start and no ``VITE_API_BASE_URL`` to configure — the SPA calls its
own origin.

Two rules make this safe:

* **Mount last.** The catch-all SPA handler is registered *after* every API
  route, so ``/health``, ``/docs``, ``/api/v1/*``, ``/v1/*`` and
  ``/openapi.json`` keep their real handlers. A mount added before them would
  swallow the API.
* **Never fake a frontend.** If ``frontend/dist`` is missing (a bare backend
  checkout, or a dev run with Vite), nothing is mounted and ``/`` keeps
  redirecting to ``/docs`` with a log line explaining why. A 200 with an empty
  HTML shell would look like a broken app.

Unknown paths get ``index.html`` so client-side routes (``/providers``,
``/logs``...) survive a refresh, but only for GET/HEAD requests that accept
HTML: a missing ``/api/v1/typo`` must still be a JSON 404, not the SPA.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.staticfiles import StaticFiles

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Directory name of the built SPA inside the frontend package.
DIST_DIRNAME = "dist"
#: ``<repo>`` — the parent of the ``backend`` package directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Marker attribute so a second call cannot mount the SPA twice (tests call
#: ``create_app``/``mount_frontend`` repeatedly on the same module state).
_MOUNTED_FLAG = "_routellm_frontend_mounted"
#: ``app.state`` key holding the outcome, for the startup log and the banner.
STATUS_ATTR = "frontend_status"
#: Extra request attributes used by the response path to tag SPA responses.
_SPA_RESPONSE_HEADER = "X-RouteLLM-Frontend"
_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
_INDEX_CACHE_CONTROL = "no-cache"


def frontend_dist_dir() -> Optional[Path]:
    """The built frontend directory, or ``None`` when it is not available.

    Resolution order: ``FRONTEND_DIST_DIR`` (explicit override), then
    ``<repo>/frontend/dist``. A directory only counts when it actually holds
    ``index.html`` — an interrupted build leaves an empty ``dist/`` behind.
    """
    candidates: list[Path] = []
    override = (settings.FRONTEND_DIST_DIR or "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(_REPO_ROOT / "frontend" / DIST_DIRNAME)

    for candidate in candidates:
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        if (candidate / "index.html").is_file():
            return candidate

    return None


class _ImmutableAssets(StaticFiles):
    """Static files whose filenames are content-hashed, so they never change.

    Vite emits ``assets/index-<hash>.js``; a year-long ``immutable`` cache is
    correct and makes reloads instant. ``index.html`` is deliberately NOT here —
    it is served with ``no-cache`` so a rebuild is picked up immediately.
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        response.headers.setdefault("cache-control", _ASSET_CACHE_CONTROL)
        return response


def _wants_html(request: Request) -> bool:
    """True for browser navigations/refreshes, False for fetch/XHR/JSON."""
    accept = request.headers.get("accept", "")
    return "text/html" in accept


def mount_frontend(app: FastAPI, dist_dir: Optional[Path] = None) -> Optional[Path]:
    """Mount the built SPA on ``app``. Returns the directory used, or ``None``.

    Must be called **after** the API routers are registered. Idempotent: a
    second call is a no-op that returns the already-mounted directory.
    """
    if getattr(app.state, _MOUNTED_FLAG, False):
        return getattr(app.state, "frontend_dir", None)

    if not settings.SERVE_FRONTEND:
        _set_status(app, mounted=False, reason="serving_disabled")
        return None

    directory = dist_dir or frontend_dist_dir()
    if directory is None:
        _set_status(app, mounted=False, reason="not_built")
        return None

    index_file = directory / "index.html"
    assets_dir = directory / "assets"

    if assets_dir.is_dir():
        # Hashed filenames: safe to cache for a year. Mounted before the
        # catch-all so /assets/* is served by Starlette's file handler.
        app.mount(
            "/assets",
            _ImmutableAssets(directory=assets_dir),
            name="frontend-assets",
        )

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        """The dashboard entry point."""
        return FileResponse(
            index_file,
            media_type="text/html",
            headers={
                "Cache-Control": _INDEX_CACHE_CONTROL,
                _SPA_RESPONSE_HEADER: "index",
            },
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str, request: Request) -> Response:
        """Client-side routing: unknown GETs render the SPA, everything else 404s.

        Files that exist in ``dist`` (``favicon.svg``, ``robots.txt``,
        ``manifest.webmanifest``) are served directly. Requests that clearly
        want JSON — an API typo, a fetch, curl — get a JSON 404 instead of an
        HTML document they cannot parse.
        """
        normalized = full_path.strip("/")
        if normalized:
            candidate = (directory / normalized).resolve()
            try:
                inside = candidate.is_relative_to(directory.resolve())
            except AttributeError:  # pragma: no cover - Python < 3.9
                inside = str(candidate).startswith(str(directory.resolve()))
            if inside and candidate.is_file():
                return FileResponse(candidate)

        if not _wants_html(request):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        return FileResponse(
            index_file,
            media_type="text/html",
            headers={
                "Cache-Control": _INDEX_CACHE_CONTROL,
                _SPA_RESPONSE_HEADER: "fallback",
            },
        )

    app.state.frontend_dir = directory
    setattr(app.state, _MOUNTED_FLAG, True)
    _set_status(app, mounted=True, reason=None, directory=directory)
    return directory


def install_root_status_page(app: FastAPI) -> None:
    """``/`` in API-only mode: a tiny HTML page linking to the useful paths.

    Replaces the old redirect-to-``/docs`` so a browser visit explains the state
    ("frontend not built") instead of silently landing on Swagger.
    """
    app.router.routes = [
        route
        for route in app.router.routes
        if not (
            getattr(route, "path", None) == "/"
            and "GET" in getattr(route, "methods", set())
        )
    ]

    @app.get("/", include_in_schema=False)
    async def _status() -> HTMLResponse:  # pragma: no cover - trivial HTML
        body = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>RouteLLM API</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:40rem;"
            "margin:4rem auto;padding:0 1.5rem;line-height:1.6;color:#1f2937}"
            "code{background:#f3f4f6;padding:.15rem .35rem;border-radius:.25rem}"
            "a{color:#b45309}</style></head><body>"
            "<h1>RouteLLM API is running</h1>"
            "<p>The backend is up, but <code>frontend/dist</code> is not built, "
            "so there is no dashboard to serve yet.</p>"
            "<p>Build it, then reload this page:</p>"
            "<p><code>cd frontend &amp;&amp; npm ci &amp;&amp; npm run build</code></p>"
            "<p>Or run the dev server: <code>python run.py --dev</code></p>"
            "<ul>"
            "<li><a href='/health'>Health &amp; dependency status</a></li>"
            "<li><a href='/docs'>Interactive API docs</a></li>"
            "<li><a href='/api/v1/catalog'>Model catalog</a></li>"
            "</ul></body></html>"
        )
        return HTMLResponse(body)


def _set_status(
    app: FastAPI,
    *,
    mounted: bool,
    reason: Optional[str],
    directory: Optional[Path] = None,
) -> None:
    """Record the outcome on ``app.state`` for the startup log/banner.

    Deliberately **not** logged here: import time must stay quiet (tools and
    tests import ``app.main`` and parse stdout). ``app.main.lifespan`` emits
    exactly one line about the dashboard.
    """
    display: Optional[str] = None
    if directory is not None:
        # Only internal paths are shown, so a shared log never contains the
        # user's home directory.
        try:
            display = f"./{directory.relative_to(_REPO_ROOT)}"
        except ValueError:
            display = str(directory)
    setattr(
        app.state,
        STATUS_ATTR,
        {"mounted": mounted, "reason": reason, "directory": display},
    )


def frontend_status(app: FastAPI) -> dict:
    """``{"mounted": bool, "reason": str|None, "directory": str|None}``."""
    return getattr(app.state, STATUS_ATTR, {"mounted": False, "reason": "unknown", "directory": None})
