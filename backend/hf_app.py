"""Hugging Face Space entrypoint (Gradio SDK).

Hugging Face runs ``python hf_app.py`` and expects the process to listen on
port 7860. This module keeps the whole existing API surface at its normal
paths and adds two conveniences on the Space:

``/``    → redirects to the status page
``/ui``  → a tiny Gradio status screen linking to ``/docs`` and ``/health``

The name is ``hf_app.py`` (not ``app.py``) on purpose: ``app/`` is the real
FastAPI package and a root-level ``app.py`` would shadow it on sys.path.

Two modes, chosen automatically and logged at startup:

``gradio``  FastAPI + the status page mounted with ``gr.mount_gradio_app``.
``plain``   FastAPI only. Used when Gradio is missing, fails to import, or
            raises while mounting (dependency conflicts). The Space still
            works: the Gradio *SDK* only requires the process to listen on
            7860 — a FastAPI app satisfies that.

IMPORTANT: ``gradio.mount_gradio_app`` swaps ``app.router.lifespan_context``
for a wrapper of its own. In Gradio 6 that wrapper DOES chain the previous
handler (verified in the source and covered by
``tests/test_hf_app.py::test_mount_keeps_startup_events_exactly_once``), so the
app's startup events (schema creation, ML probe) still run. Wrapping it a
second time here would run them twice — which is exactly the bug that test
guards against.
"""

from __future__ import annotations

import os
import sys
from typing import Literal, Optional

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.main import app, lifespan as app_lifespan, log_startup_diagnostics
from app.runtime import running_on_huggingface
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Hugging Face (Gradio SDK) hard-codes this port. PORT exists for local runs.
DEFAULT_PORT = 7860
#: The status page mount point.
STATUS_UI_PATH = "/ui"

SdkMode = Literal["gradio", "plain"]

#: Set after ``configure_app()`` so the running mode is introspectable
#: (startup log + tests).
ACTIVE_MODE: Optional[SdkMode] = None


def build_status_ui():
    """Return ``(ui, gradio_module)`` for the status page.

    Raises ImportError when Gradio is unavailable and propagates anything Gradio
    raises while building — the caller falls back to plain mode, so a broken
    Gradio install can never take the API down.
    """
    import gradio as gr

    with gr.Blocks(title="RouteLLM API") as ui:
        gr.Markdown(
            "# RouteLLM API is running\n"
            "The routing gateway for this Space is live.\n\n"
            "- [Interactive API docs](/docs)\n"
            "- [Health & dependency status](/health)\n"
            "- [Model catalog](/api/v1/catalog)\n\n"
            "_The dashboard UI is hosted separately on Cloudflare Workers. "
            "This page only confirms the backend is up._"
        )
    return ui, gr


def mount_status_page(target_app: FastAPI, ui=None, gr=None) -> bool:
    """Mount the Gradio status page at /ui. Returns True when it worked.

    Also asserts that mounting did not orphan the app's startup events —
    Gradio has changed this behavior across releases, and a silently dropped
    lifespan means tables are never created.
    """
    if ui is None or gr is None:
        ui, gr = build_status_ui()

    before = target_app.router.lifespan_context
    gr.mount_gradio_app(target_app, ui, path=STATUS_UI_PATH)
    chained = target_app.router.lifespan_context is not before
    logger.info(
        "hf_app_gradio_mounted",
        path=STATUS_UI_PATH,
        lifespan_chained=chained,
    )
    if not chained:
        logger.error(
            "hf_app_lifespan_not_chained",
            detail="Gradio replaced the app lifespan without chaining it; "
            "startup events (schema creation) would not run.",
        )
        return False
    return True


def install_root_redirect(target_app: FastAPI, mode: SdkMode) -> None:
    """Point ``/`` at the status page (gradio) or the API docs (plain).

    Any previously registered ``/`` handler is removed first: FastAPI matches
    routes in registration order, so leaving the old one would win.
    """
    target_app.router.routes = [
        route
        for route in target_app.router.routes
        if not (
            getattr(route, "path", None) == "/"
            and "GET" in getattr(route, "methods", set())
        )
    ]

    @target_app.get("/", include_in_schema=False)
    async def _root():  # pragma: no cover - exercised over HTTP in tests
        return RedirectResponse(
            url=STATUS_UI_PATH if mode == "gradio" else "/docs", status_code=307
        )


def configure_app(mode: SdkMode = "gradio", target_app: Optional[FastAPI] = None):
    """Prepare the app for serving. Returns the *effective* mode.

    Never raises: any failure to mount Gradio degrades to ``plain`` mode with
    a clear log line, because a working API beats a pretty landing page.
    """
    global ACTIVE_MODE

    target = target_app or app
    effective: SdkMode = "plain"

    if mode == "gradio":
        try:
            if mount_status_page(target):
                effective = "gradio"
        except ImportError as exc:
            logger.warning(
                "hf_app_gradio_unavailable",
                mode="plain",
                error=str(exc)[:300],
            )
        except Exception as exc:  # version conflict, starlette mismatch, ...
            logger.error(
                "hf_app_gradio_mount_failed",
                mode="plain",
                error=str(exc)[:300],
            )

    install_root_redirect(target, effective)
    ACTIVE_MODE = effective
    log_startup_diagnostics(mode=effective)
    logger.info(
        "hf_app_ready",
        mode=effective,
        on_huggingface=running_on_huggingface(),
        port=_resolve_port(),
        api_paths=["/health", "/docs", "/api/v1/*", "/v1/*", STATUS_UI_PATH],
    )
    return effective


def _resolve_port() -> int:
    """Port from ``PORT`` (Hugging Face injects it) with a 7860 default."""
    raw = os.environ.get("PORT", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning("invalid_port_env", value=raw[:20], fallback=DEFAULT_PORT)
    return DEFAULT_PORT


def main(argv: Optional[list[str]] = None) -> int:
    """Start uvicorn on 0.0.0.0:<PORT>. Returns a process exit code."""
    import uvicorn

    argv = list(sys.argv[1:] if argv is None else argv)
    wanted: SdkMode = "plain" if "--plain" in argv else "gradio"
    configure_app(wanted)
    port = _resolve_port()

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        # Hugging Face terminates TLS in front of the container, so uvicorn
        # must trust the forwarded headers to build correct absolute URLs
        # (redirects to /ui, OpenAPI servers, client IPs for rate limiting).
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        # No reload, no workers: the Gradio SDK expects one process on 7860.
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
