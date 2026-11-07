"""Hugging Face Space entrypoint for RouteLLM (Gradio SDK, ZeroGPU-compatible).

PROBLEM 1 root cause (reproduced locally, see DIAGNOSIS in DEPLOY.md and the
pytest regression ``test_hf_entrypoint``): the previous Space entrypoint
started more than one server on port 7860 inside the same process. Uvicorn
runs the ASGI lifespan BEFORE binding the socket, so the logs showed
``database_schema_ready`` → ``Application startup complete`` →
``ERROR: [Errno 98] error while attempting to bind on address ('0.0.0.0',
7860): address already in use`` → shutdown → HF restart → crash loop.

THE FIX — the server is started in exactly ONE place:

  * This module is import-safe: importing it defines nothing heavier than
    functions and binds NO port, starts NO thread. importing it twice (the
    runtime's probe import + ``python hf_app.py`` execution) cannot race for
    7860 — only ``__main__`` starts a server, and only one.
  * On the HF Gradio SDK (gradio + spaces installed): the Gradio app (a small
    status/landing UI) is mounted into the FastAPI gateway with
    ``gr.mount_gradio_app`` at ``/ui``, then the ONE uvicorn server binds
    0.0.0.0:7860 and serves every route at its normal path —
    /, /health, /docs, /api/v1/*, /v1/* — plus the /ui status page.
  * Without gradio/spaces (local dev, CI): plain FastAPI + uvicorn.
  * No ``@spaces.GPU`` functions exist: this gateway does CPU-only work
    (routing, retries, telemetry), so ZeroGPU is simply never requested.
"""

from __future__ import annotations

import os
import sys
import threading

# Local-dev ergonomics (no-op on the Space, where this file sits NEXT to the
# staged `app/` package): allow `python hf_app.py` from the repo root by
# putting backend/ on sys.path so `app.main` resolves either way. Import-time
# side effects stay nil — this binds nothing, starts nothing.
_BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")
if os.path.isdir(_BACKEND_DIR) and _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

# ----------------------------------------------------------------------------
# Lightweight startup diagnostics — ALWAYS on (two cheap lines), heavy port
# ownership forensics only fires when a bind failure actually happens. This is
# the "demoted" version of the temporary diagnostics used to fix Problem 1.
# ----------------------------------------------------------------------------


def _startup_probe(port: int) -> None:
    try:
        import socket

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("0.0.0.0", port))
            state = "free"
        finally:
            probe.close()
    except OSError:
        state = "busy"
    print(
        f"[hf_app] pid={os.getpid()} name={__name__} "
        f"threads={len(threading.enumerate())} port={port} {state}",
        flush=True,
    )
    if state == "busy":
        _trace_port_owner(port)


def _trace_port_owner(port: int) -> None:
    """Log which PID owns the port (Linux: /proc; anywhere: psutil).

    Never prints secrets — only PIDs, argv basenames and socket tables.
    """
    try:
        import psutil  # type: ignore

        for conn in psutil.net_connections(kind="tcp"):
            if conn.laddr and conn.laddr.port == port and conn.status == "LISTEN":
                try:
                    proc = psutil.Process(conn.pid)
                    cmdline = proc.cmdline() or [proc.name()]
                    safe = [os.path.basename(c) for c in cmdline]
                    print(
                        f"[hf_app] port {port} OWNER pid={conn.pid} "
                        f"argv={safe}",
                        flush=True,
                    )
                except Exception:
                    print(
                        f"[hf_app] port {port} owner pid={conn.pid} "
                        "(details unavailable)",
                        flush=True,
                    )
        return
    except ImportError:
        pass
    # Linux fallback without psutil: /proc/net/tcp sinistates + /proc/*/fd.
    try:
        port_hex = f"{port:04X}"
        inodes: set[str] = set()
        path = "/proc/net/tcp"
        if os.path.exists(path):
            with open(path, encoding="ascii") as f:
                next(f)  # header
                for line in f:
                    parts = line.split()
                    local = parts[1]
                    if local.rsplit(":", 1)[-1] == port_hex and parts[3] == "0A":
                        inodes.add(parts[9])
        if not inodes:
            print(f"[hf_app] port {port} owner: not found in {path}", flush=True)
            return
        for pid_dir in os.listdir("/proc"):
            if not pid_dir.isdigit():
                continue
            fd_dir = f"/proc/{pid_dir}/fd"
            try:
                for fd in os.listdir(fd_dir):
                    try:
                        link = os.readlink(os.path.join(fd_dir, fd))
                    except OSError:
                        continue
                    if link.startswith("socket:[") and link[8:-1] in inodes:
                        cmd_path = f"/proc/{pid_dir}/cmdline"
                        try:
                            with open(cmd_path, encoding="utf-8", errors="replace") as f:
                                argv = f.read().replace("\x00", " ").strip()
                            argv = " ".join(
                                os.path.basename(part) for part in argv.split()
                            )
                        except OSError:
                            argv = "(unreadable)"
                        print(
                            f"[hf_app] port {port} OWNER pid={pid_dir} argv={argv}",
                            flush=True,
                        )
                        return
            except OSError:
                continue
    except Exception as exc:  # diagnostics must never be fatal
        print(f"[hf_app] port owner forensics failed: {type(exc).__name__}", flush=True)


SERVER_PORT = int(os.environ.get("HF_PORT", os.environ.get("PORT", "7860")))
SERVER_NAME = os.environ.get("HF_SERVER_NAME", "0.0.0.0")


def _build_status_demo():
    """Tiny Gradio Blocks served at /ui — the Space status/landing page.

    Static on purpose: it must render even when Neon/Upstash are still waking
    up. The live numbers live in the dashboard and under /docs.
    """
    import gradio as gr

    with gr.Blocks(title="RouteLLM — Space status") as demo:
        gr.Markdown(
            f"""
# RouteLLM gateway is up
**Space:** `{os.environ.get("SPACE_ID", "local")}` · **Port:** {SERVER_PORT}

RouteLLM is a FastAPI model-routing gateway (classifier → scorer → fallback
execution) with per-caller API keys and BYOK provider keys.

| What | Where |
|---|---|
| API docs (OpenAPI / Swagger) | [/docs](/docs) |
| Health probe | [/health](/health) |
| Dashboard frontend | [routellm.saboornasar66.workers.dev](https://routellm.saboornasar66.workers.dev) |
| OpenAI-compatible chat endpoint | `POST /v1/chat/completions` |
| REST routing API | `POST /api/v1/route` |

The dashboard is served from its own Cloudflare Worker and proxies requests to
this Space server-side — no CORS involved in the primary topology.
"""
        )
    return demo


def build_app():
    """Return the ASGI app to serve. Import-safe: binds nothing.

    Gradio available → status demo mounted at /ui onto the FastAPI gateway
    (gradio can annotate GPU stubs; no uvicorn is started by mounting).
    Gradio missing  → the plain FastAPI gateway (local dev / CI).
    """
    import uvicorn  # noqa: F401  (re-exported for the deploy script contract)

    try:
        import gradio as gr
    except ImportError:
        print("[hf_app] gradio not installed — plain FastAPI mode", flush=True)
        from app.main import app

        return app

    try:
        import spaces  # noqa: F401  must precede gradio on ZeroGPU
    except ImportError:
        print("[hf_app] spaces not installed — fine outside HF ZeroGPU", flush=True)

    from app.main import app

    demo = _build_status_demo()
    app_with_ui = gr.mount_gradio_app(app, demo, path="/ui")
    print(
        "[hf_app] hf_app_gradio_mounted",
        f"path=/ui lifespan_chained=True gradio={gr.__version__}",
        flush=True,
    )
    return app_with_ui


def main() -> None:
    _startup_probe(SERVER_PORT)

    from app.main import app  # noqa: F401

    asgi = build_app()
    import uvicorn

    try:
        # THE single bind/serve step for the whole process.
        uvicorn.run(
            asgi,
            host=SERVER_NAME,
            port=SERVER_PORT,
            log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        )
    except OSError as exc:
        # Match the production symptom with an actionable diagnosis.
        print(
            f"[hf_app] BIND FAILED on ({SERVER_NAME!r}, {SERVER_PORT}): {exc}\n"
            f"[hf_app] Ensure only ONE server starts per process: this "
            f"entrypoint is the only place that calls uvicorn.run. "
            f"Is a previous instance still running?",
            flush=True,
        )
        _trace_port_owner(SERVER_PORT)
        raise


if __name__ == "__main__":
    main()
