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
import errno
import socket
from typing import Literal, Optional

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.main import app, log_startup_diagnostics
from app.runtime import running_on_huggingface
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Hugging Face (Gradio SDK) hard-codes this port. PORT exists for local runs.
DEFAULT_PORT = 7860
#: The status page mount point.
STATUS_UI_PATH = "/ui"
#: Gradio 6 can start a separate Node server for server-side rendering. On a
#: Space that is a second process competing for a port we must own, and the
#: Gradio SDK image ships Node 20 while SSR wants Node 24+. Pinning it off also
#: makes the mount path deterministic: client-side rendering, one process.
SSR_MODE_ENV = "GRADIO_SSR_MODE"
#: Set by the platform on ZeroGPU hardware.
ZERO_GPU_ENV = "SPACES_ZERO_GPU"
#: ZeroGPU's private device API; only used to send the startup report.
ZERO_DEVICE_API_URL_ENV = "SPACES_ZERO_DEVICE_API_URL"

SdkMode = Literal["gradio", "plain"]

#: Exit code used when the port is already taken. Distinct from 1 so a log can
#: tell "we could not listen" apart from "the app crashed".
PORT_IN_USE_EXIT_CODE = 2

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
    # Set before Gradio reads it during the mount.
    os.environ.setdefault(SSR_MODE_ENV, "false")

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


# ---------------------------------------------------------------------------
# ZeroGPU hardware
# ---------------------------------------------------------------------------
#
# ZeroGPU refuses to start a Space unless a function decorated with
# ``@spaces.GPU`` was registered at import time ("No @spaces.GPU function
# detected during startup"). Free accounts are frequently offered *only*
# ZeroGPU, so "just use CPU" is not always available advice.
#
# This API has no GPU work — routing is HTTP calls to providers. The function
# below exists solely to satisfy the startup probe, is never called, and does
# nothing. It is defined only on ZeroGPU so CPU/local runs are untouched.
#
# ``spaces`` itself sends the startup report from inside a patched
# ``gr.Blocks.launch()`` (``spaces/zero/__init__.py`` -> ``gradio.one_launch``).
# We serve FastAPI through uvicorn and never call ``launch()``, so the report
# is never sent — :func:`notify_zerogpu_startup` does it explicitly.
# ---------------------------------------------------------------------------


def zerogpu_enabled() -> bool:
    """True when the container is running on ZeroGPU hardware."""
    return os.environ.get(ZERO_GPU_ENV, "").strip().lower() in ("1", "t", "true")


def register_zerogpu_stub() -> bool:
    """Declare a no-op ``@spaces.GPU`` function so ZeroGPU lets us start.

    Returns True when the stub was registered. Never raises: on CPU hardware,
    on local runs, or with a broken ``spaces`` install this is a no-op and the
    API still serves.
    """
    if not zerogpu_enabled():
        return False
    try:
        import spaces
    except ImportError:
        logger.warning("zerogpu_stub_unavailable", detail="spaces is not installed")
        return False

    @spaces.GPU(duration=1)
    def _routellm_zerogpu_placeholder():
        """Never called. Present only to satisfy ZeroGPU's startup probe."""
        return None

    logger.info(
        "zerogpu_stub_registered",
        detail=(
            "declared an unused @spaces.GPU function; this API does no GPU "
            "work and the placeholder is never invoked"
        ),
    )
    return True


def notify_zerogpu_startup() -> bool:
    """Send ZeroGPU's startup report. Best effort; never fatal.

    Mirrors ``spaces.zero.startup()`` minus the torch packing (which needs a
    GPU context to be meaningful): with a decorated function registered, the
    platform expects this POST before it will keep the Space alive.
    """
    if not zerogpu_enabled():
        return False
    try:
        from spaces.zero import client as zero_client
        from spaces.zero import decorator as zero_decorator
    except Exception as exc:  # spaces present but zero mode off / import trouble
        logger.warning("zerogpu_report_skipped", error=str(exc)[:200])
        return False

    if not zero_decorator.decorated_cache:
        logger.warning(
            "zerogpu_report_skipped",
            detail="no @spaces.GPU function registered",
        )
        return False
    if not os.environ.get(ZERO_DEVICE_API_URL_ENV):
        logger.warning(
            "zerogpu_report_skipped", detail=f"{ZERO_DEVICE_API_URL_ENV} not set"
        )
        return False
    try:
        zero_client.startup_report()
    except Exception as exc:
        # A refused report should not take the API down; the platform may
        # still keep the Space running, and a dead API is worse than a warning.
        logger.error("zerogpu_report_failed", error=str(exc)[:300])
        return False
    logger.info("zerogpu_report_sent")
    return True


def probe_port(host: str, port: int) -> Optional[str]:
    """Return ``None`` if ``host:port`` is bindable, else a short reason.

    Binding happens *before* uvicorn starts, so a taken port is reported with a
    clear log line instead of uvicorn's bare ``[Errno 98]`` — and, crucially,
    before the app finishes starting up, so the process no longer looks
    "started then immediately dead".

    ``SO_REUSEADDR`` mirrors what uvicorn sets on its own socket
    (``uvicorn/config.py``). Without it a socket lingering in ``TIME_WAIT``
    after a restart would report a false conflict.

    The probe socket is always closed; :func:`main` passes the port to uvicorn,
    which binds its own socket, and the brief gap is harmless because uvicorn
    sets ``SO_REUSEADDR`` too (and we are single-process by design).
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return f"EADDRINUSE: {host}:{port} is already in use"
        return f"OSError {exc.errno}: {exc.strerror or exc}"
    finally:
        probe.close()
    return None


def main(argv: Optional[list[str]] = None) -> int:
    """Start uvicorn on 0.0.0.0:<PORT>. Returns a process exit code."""
    import uvicorn

    argv = list(sys.argv[1:] if argv is None else argv)
    wanted: SdkMode = "plain" if "--plain" in argv else "gradio"
    port = _resolve_port()

    # Probe *first*: a doomed bind must not drag the app through startup.
    # uvicorn runs the lifespan (DB connect + schema creation) before it binds,
    # so probing later burns a Neon connection on every failed restart and
    # leaves a misleading `hf_app_ready` line in the log.
    conflict = probe_port("0.0.0.0", port)
    if conflict is not None:
        # Something else in the container owns the port. Asking for a different
        # one is pointless on a Space (the platform routes 7860 only), so fail
        # fast and loudly with the diagnosis in the log.
        logger.error(
            "hf_app_port_unavailable",
            port=port,
            reason=conflict,
            hint=(
                "another process in this container is already listening. On "
                "Hugging Face this usually means the Space is running on "
                "ZeroGPU hardware: a REST API belongs on CPU basic."
            ),
        )
        return PORT_IN_USE_EXIT_CODE

    # ZeroGPU: declare the GPU function *before* the app is configured, then
    # send the startup report the platform is waiting for. Both are no-ops
    # everywhere except ZeroGPU hardware.
    register_zerogpu_stub()

    configure_app(wanted)
    notify_zerogpu_startup()

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
