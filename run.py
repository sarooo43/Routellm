#!/usr/bin/env python3
"""RouteLLM launcher — clone, run one command, get the app.

    python run.py                 # build/install what is missing, then serve
    python run.py --dev           # backend reload + Vite dev server
    python run.py --no-browser    # do not open a browser window
    python run.py --port 8123     # pick a port (default 8000, auto-advances)
    python run.py --host 0.0.0.0  # EXPOSE TO YOUR NETWORK (see the warning)

What it does, in order:

1. Checks the Python version (3.11+).
2. Creates ``.venv`` and installs ``backend/requirements.txt`` into it, only
   when something is missing or out of date (``--reinstall`` forces it).
3. Uses ``frontend/dist`` when present; otherwise builds it with npm (Node 20+),
   or explains how to get a prebuilt release zip.
4. Creates the data directory and, on first run only, ``backend/.env`` from
   ``backend/.env.example``.
5. Starts uvicorn on 127.0.0.1:8000 and opens the dashboard.

This file is intentionally **stdlib-only** and import-safe: it must run on a
bare Python install, before any dependency exists. Everything that needs the
venv is executed in a subprocess with that interpreter.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import venv
from pathlib import Path
from typing import Optional, Sequence

MIN_PYTHON = (3, 11)
DEFAULT_PORT = 8000
DEFAULT_HOST = "127.0.0.1"
REPO_ROOT = Path(__file__).resolve().parent
BACKEND_DIR = REPO_ROOT / "backend"
FRONTEND_DIR = REPO_ROOT / "frontend"
DIST_DIR = FRONTEND_DIR / "dist"
VENV_DIR = REPO_ROOT / ".venv"
REQUIREMENTS = BACKEND_DIR / "requirements.txt"
REQUIREMENTS_STAMP = VENV_DIR / ".routellm-requirements.sha256"
ENV_EXAMPLE = BACKEND_DIR / ".env.example"
ENV_FILE = BACKEND_DIR / ".env"
DATA_DIR = REPO_ROOT / "data"

#: ANSI colours, disabled when the stream is not a TTY (CI logs, Windows cmd).
_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

# Windows consoles default to a legacy codepage (cp1252 on most Western
# locales) that cannot encode the launcher's checkmark glyphs — the very
# first status line then died with UnicodeEncodeError before anything ran.
# Reconfigure to UTF-8 with replacement so the launcher works on ANY console,
# including piped output. Best-effort: odd embedded interpreters may refuse.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError, OSError):  # pragma: no cover - exotic streams
    pass


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(text: str) -> str:
    return _c("1", text)


def dim(text: str) -> str:
    return _c("2", text)


def ok(text: str) -> str:
    return _c("32", text)


def warn(text: str) -> str:
    return _c("33", text)


def fail(text: str) -> str:
    return _c("31", text)


class LauncherError(RuntimeError):
    """A failure with a message meant for the person running the script."""


# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------


def check_python() -> None:
    """Refuse to continue on a Python the backend cannot run on."""
    if sys.version_info < MIN_PYTHON:
        current = ".".join(str(part) for part in sys.version_info[:3])
        raise LauncherError(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required "
            f"(this is {current}).\n"
            "  Windows: https://www.python.org/downloads/windows/ (tick "
            "'Add python.exe to PATH')\n"
            "  macOS:   brew install python@3.12\n"
            "  Linux:   sudo apt install python3.12 python3.12-venv"
        )


def venv_python() -> Path:
    """Path to the interpreter inside ``.venv`` for this platform."""
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def venv_bin(name: str) -> Path:
    """Path to an executable inside ``.venv`` for this platform."""
    return VENV_DIR / ("Scripts" if os.name == "nt" else "bin") / name


def environment_report() -> list[tuple[bool, str]]:
    """What `--check` prints: one line per prerequisite, nothing changed.

    Deliberately read-only. A probe that installs dependencies is not a probe —
    it is an install with extra steps, and it makes CI (or a curious user) pay
    for a full build just to answer "is this machine ready?".
    """
    lines: list[tuple[bool, str]] = []

    current = ".".join(str(part) for part in sys.version_info[:3])
    lines.append(
        (sys.version_info >= MIN_PYTHON, f"Python {current} (need {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+)")
    )

    python = venv_python()
    if python.exists() and _requirements_up_to_date():
        lines.append((True, f"virtualenv ready ({VENV_DIR.name})"))
    elif python.exists():
        lines.append((False, "virtualenv exists but dependencies changed — will install"))
    else:
        lines.append((False, "virtualenv missing — will be created and installed"))

    if dist_is_usable():
        lines.append((True, "dashboard build found (frontend/dist)"))
    else:
        version = node_version()
        if version and version[0] >= 20:
            lines.append((False, f"dashboard not built — will build with Node {version[0]}"))
        elif version:
            lines.append((False, f"dashboard not built and Node {version[0]} is too old (need 20+)"))
        else:
            lines.append((False, "dashboard not built and Node.js is missing (API still runs)"))

    lines.append(
        (
            ENV_FILE.exists(),
            "backend/.env present" if ENV_FILE.exists() else "backend/.env will be created from the example",
        )
    )
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        lines.append((True, f"data directory {DATA_DIR}"))
    except OSError as exc:
        lines.append((False, f"data directory {DATA_DIR} is not writable ({exc.strerror})"))
    return lines


def ensure_venv(reinstall: bool = False) -> Path:
    """Create ``.venv`` if needed and install requirements when they changed.

    Returns the venv interpreter. Idempotent: a second run costs a hash
    comparison, not a dependency install.
    """
    python = venv_python()
    if not python.exists():
        print(f"  {dim('creating virtual environment ./.venv')}")
        try:
            venv.EnvBuilder(with_pip=True, clear=False, upgrade_deps=False).create(
                str(VENV_DIR)
            )
        except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the user
            raise LauncherError(
                f"Could not create the virtual environment: {exc}\n"
                "  On Debian/Ubuntu install the venv package: "
                "sudo apt install python3-venv"
            ) from exc

    if not python.exists():  # pragma: no cover - broken venv layout
        raise LauncherError(
            f"The virtual environment at {VENV_DIR} is incomplete. "
            "Delete it and rerun: rm -rf .venv"
        )

    if not reinstall and _requirements_up_to_date():
        return python

    print(f"  {dim('installing backend dependencies (first run only, ~1 minute)')}")
    _run(
        [str(python), "-m", "pip", "install", "--upgrade", "pip", "--quiet"],
        what="pip self-upgrade",
        quiet=True,
    )
    _run(
        [str(python), "-m", "pip", "install", "-r", str(REQUIREMENTS), "--quiet"],
        what="dependency installation",
    )
    _write_requirements_stamp()
    return python


def _requirements_hash() -> str:
    import hashlib

    digest = hashlib.sha256()
    for path in (REQUIREMENTS,):
        digest.update(path.read_bytes())
    digest.update(sys.platform.encode())
    return digest.hexdigest()


def _requirements_up_to_date() -> bool:
    if not REQUIREMENTS_STAMP.exists():
        return False
    try:
        return REQUIREMENTS_STAMP.read_text(encoding="utf-8").strip() == _requirements_hash()
    except OSError:
        return False


def _write_requirements_stamp() -> None:
    try:
        REQUIREMENTS_STAMP.write_text(_requirements_hash(), encoding="utf-8")
    except OSError:  # pragma: no cover - read-only venv
        pass


def _run(
    command: Sequence[str],
    *,
    what: str,
    cwd: Optional[Path] = None,
    quiet: bool = False,
    env: Optional[dict] = None,
) -> None:
    """Run a subprocess, turning a non-zero exit into a readable LauncherError."""
    try:
        result = subprocess.run(
            list(command),
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.DEVNULL if quiet else None,
            stderr=subprocess.STDOUT if quiet else None,
            check=False,
        )
    except FileNotFoundError as exc:
        raise LauncherError(
            f"{what} failed: {command[0]!r} is not installed or not on PATH."
        ) from exc
    if result.returncode != 0:
        raise LauncherError(
            f"{what} failed (exit {result.returncode}).\n"
            f"  Command: {' '.join(str(part) for part in command)}"
        )


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------


def node_version() -> Optional[tuple[int, ...]]:
    """Installed Node major/minor version, or None when Node is absent."""
    binary = shutil.which("node")
    if not binary:
        return None
    try:
        raw = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, check=False
        ).stdout.strip()
    except OSError:  # pragma: no cover - exotic environments
        return None
    if not raw.startswith("v"):
        return None
    try:
        return tuple(int(part) for part in raw[1:].split("."))
    except ValueError:
        return None


def dist_is_usable() -> bool:
    """A ``dist/`` only counts when the build actually produced index.html."""
    return (DIST_DIR / "index.html").is_file()


def ensure_frontend(dev: bool = False, rebuild: bool = False) -> Optional[Path]:
    """Ensure a built dashboard exists. Returns the dist dir, or None.

    ``None`` is a legitimate result: the backend then serves a small status
    page at ``/`` explaining how to build the frontend. That is better than
    aborting a backend that works.
    """
    if dev:
        return DIST_DIR if dist_is_usable() else None
    if dist_is_usable() and not rebuild:
        return DIST_DIR

    npm = shutil.which("npm")
    version = node_version()
    if not npm or not version:
        print(
            f"  {warn('Node.js not found')} — cannot build the dashboard.\n"
            f"  {dim('Install Node 20+ (https://nodejs.org), or unzip the prebuilt')}\n"
            f"  {dim('frontend/dist from a release, then rerun. The API still works:')}\n"
            f"  {dim('http://127.0.0.1:8000/docs')}"
        )
        return None
    if version[0] < 20:
        print(
            f"  {warn(f'Node {version[0]} is too old to build the dashboard (need 20+)')}\n"
            f"  {dim('The API still works: http://127.0.0.1:8000/docs')}"
        )
        return None

    if not (FRONTEND_DIR / "node_modules").is_dir() or rebuild:
        print(f"  {dim('installing frontend dependencies (first run only, ~1 minute)')}")
        _run([npm, "ci", "--no-audit", "--no-fund"], what="npm ci", cwd=FRONTEND_DIR)
    print(f"  {dim('building the dashboard')}")
    _run([npm, "run", "build"], what="npm run build", cwd=FRONTEND_DIR)
    if not dist_is_usable():  # pragma: no cover - build should have produced it
        print(f"  {warn('the build finished but frontend/dist/index.html is missing')}")
        return None
    return DIST_DIR


# ---------------------------------------------------------------------------
# Local state
# ---------------------------------------------------------------------------


def ensure_env_file() -> bool:
    """Create ``backend/.env`` from the example on first run.

    Never overwrites an existing file: it may hold the user's provider keys.
    Returns True when a new file was created.
    """
    if ENV_FILE.exists() or not ENV_EXAMPLE.exists():
        return False
    try:
        shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
    except OSError as exc:  # pragma: no cover - read-only checkout
        print(f"  {warn(f'could not create backend/.env: {exc}')}")
        return False
    return True


def ensure_data_dir() -> Path:
    """Create the local data directory (git-ignored) ahead of the backend."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - read-only checkout; backend falls back
        return DATA_DIR
    return DATA_DIR


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


def port_is_free(host: str, port: int) -> bool:
    """True when ``host:port`` can be bound right now."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def pick_port(host: str, preferred: int, attempts: int = 20) -> tuple[int, bool]:
    """First free port at or after ``preferred``.

    Returns ``(port, was_shifted)``. Only ``--port`` values are guaranteed;
    the default shifts quietly so a second checkout just works.
    """
    for offset in range(attempts):
        candidate = preferred + offset
        if port_is_free(host, candidate):
            return candidate, candidate != preferred
    raise LauncherError(
        f"Ports {preferred}-{preferred + attempts - 1} are all in use. "
        "Stop one, or pass --port with a free one."
    )


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def print_banner(url: str, data_dir: Path, provider_hint: bool) -> None:
    """The one thing worth reading: where the app is and what to do next."""
    line = "─" * 58
    print()
    print(bold("  RouteLLM is running"))
    print(f"  {dim(line)}")
    print(f"  Dashboard   {bold(url)}")
    print(f"  API docs    {url}docs")
    print(f"  Health      {url}health")
    print(f"  Data        {data_dir}")
    print(f"  {dim(line)}")
    codespace = os.environ.get("CODESPACE_NAME", "").strip()
    domain = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "").strip()
    if codespace and domain:
        # The forwarded URL's Host (e.g. myspace-8123.app.github.dev) is NOT in
        # the middleware's loopback set, so the first request through the
        # forwarded port would 400. One line, before it happens. The middleware
        # already allows <CODESPACE_NAME>-<port>.<domain> automatically.
        port = url.split(":", 1)[1].split("/", 1)[0] if ":" in url else "8000"
        forwarded = f"{codespace}-{port}.{domain}"
        print(f"  {warn('Codespace detected:')} its forwarded Host is allowed automatically.")
        print(f"  {dim(f'  forwarded URL: https://{forwarded}')}")
    if provider_hint:
        print(f"  {warn('No provider key configured yet.')}")
        print("  Open the Providers page and add a free key:")
        print("    Groq        https://console.groq.com/keys")
        print("    Google AI   https://aistudio.google.com/app/apikey")
        print("    DeepSeek    https://platform.deepseek.com/api_keys")
        print(f"  {dim('Or use Demo mode to explore the UI with no key at all.')}")
    else:
        print("  Providers are configured. Chaos mode is on: try 'Simulate outage'.")
    print(f"  {dim(line)}")
    print(f"  Press {bold('Ctrl+C')} to stop.")
    print()


def _providers_configured(env_path: Path = ENV_FILE) -> bool:
    """True when any provider key is present in .env or the environment."""
    names = ("GROQ_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY")
    if any((os.environ.get(name) or "").strip() for name in names):
        return True
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() in names and value.strip():
            return True
    return False


def open_browser(url: str) -> None:
    """Open the dashboard without letting a failure stop the server."""
    import webbrowser

    try:
        webbrowser.open(url)
    except Exception:  # pragma: no cover - headless machines
        pass


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------


def backend_env(port: int, host: str, dev: bool) -> dict:
    """Environment for the backend process (never contains secrets)."""
    env = dict(os.environ)
    env.setdefault("ROUTELLM_DATA_DIR", str(DATA_DIR))
    env.setdefault("PORT", str(port))
    env.setdefault("HOST", host)
    if dev:
        env["VITE_DEV_SERVER"] = "1"
    # PYTHONPATH: run uvicorn from backend/ so `app` is importable regardless
    # of how the script was invoked.
    env["PYTHONPATH"] = str(BACKEND_DIR)
    return env


def serve(python: Path, host: str, port: int, reload: bool) -> int:
    """Run uvicorn in the foreground; Ctrl+C stops it cleanly."""
    command = [
        str(python),
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        "info",
    ]
    if reload:
        command += ["--reload", "--reload-dir", str(BACKEND_DIR / "app")]
    try:
        return subprocess.call(command, cwd=str(BACKEND_DIR), env=backend_env(port, host, reload))
    except KeyboardInterrupt:  # pragma: no cover - the child gets the signal
        return 0


def serve_dev_frontend(dev: bool) -> Optional[subprocess.Popen]:
    """Start the Vite dev server alongside the reloading backend."""
    if not dev:
        return None
    npm = shutil.which("npm")
    if not npm:
        print(f"  {warn('npm not found — --dev needs Node 20+; serving the API only')}")
        return None
    print(f"  {dim('starting the Vite dev server (http://localhost:5173)')}")
    try:
        return subprocess.Popen(
            [npm, "run", "dev"],
            cwd=str(FRONTEND_DIR),
            env={**os.environ, "VITE_API_BASE_URL": ""},
        )
    except OSError as exc:  # pragma: no cover
        print(f"  {warn(f'could not start the dev server: {exc}')}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python run.py",
        description="Start RouteLLM locally (no Docker, no Postgres, no Redis).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python run.py                 # http://127.0.0.1:8000\n"
            "  python run.py --dev           # backend reload + Vite dev server\n"
            "  python run.py --port 8123     # different port\n"
            "  python run.py --host 0.0.0.0  # expose to your LAN (see warning)\n"
        ),
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", DEFAULT_HOST),
        help=f"bind address (default {DEFAULT_HOST}, loopback only)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", DEFAULT_PORT) or DEFAULT_PORT),
        help=f"port (default {DEFAULT_PORT}; the next free port is used if taken)",
    )
    parser.add_argument("--dev", action="store_true", help="backend reload + Vite dev server")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument("--reinstall", action="store_true", help="force a dependency reinstall")
    parser.add_argument("--rebuild", action="store_true", help="force a frontend rebuild")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the environment and exit (no server started)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point. Returns a process exit code."""
    args = parse_args(argv)

    print()
    print(f"  {bold('RouteLLM')} {dim('local launcher')}  {dim(f'({platform.system()})')}")

    try:
        check_python()

        if args.check:
            # Read-only: report what is present and what the launcher would do.
            for ready, message in environment_report():
                mark = ok("✓") if ready else warn("·")
                print(f"  {mark} {message}")
            try:
                port, shifted = pick_port(args.host, args.port)
            except LauncherError as exc:
                print(f"  {warn('·')} {exc}")
                return 1
            host_for_url = "localhost" if args.host in ("127.0.0.1", "0.0.0.0", "::1") else args.host
            print()
            print(f"  Nothing was installed or started.")
            print(f"  `python run.py` would serve http://{host_for_url}:{port}/"
                  + (f"  (port {args.port} is busy)" if shifted else ""))
            print()
            return 0

        print(f"  {ok('✓')} Python {sys.version.split()[0]}")

        python = ensure_venv(reinstall=args.reinstall)
        print(f"  {ok('✓')} Backend dependencies ready")

        dist = ensure_frontend(dev=args.dev, rebuild=args.rebuild)
        if dist:
            print(f"  {ok('✓')} Dashboard build found")
        elif not args.dev:
            print(f"  {warn('!')} Dashboard not built — the API still runs")

        created_env = ensure_env_file()
        if created_env:
            print(f"  {ok('✓')} Created backend/.env from the example (all defaults)")
        data_dir = ensure_data_dir()
        print(f"  {ok('✓')} Data directory {data_dir}")

        if args.host not in ("127.0.0.1", "localhost", "::1"):
            print()
            print(f"  {warn('WARNING: binding to ' + args.host)}")
            print(
                f"  {warn('Anyone on your network can use this gateway and spend your')}\n"
                f"  {warn('provider credits. Use loopback unless you mean it.')}"
            )

        port, shifted = pick_port(args.host, args.port)
        if shifted:
            print(f"  {warn(f'!')} Port {args.port} is busy — using {port} instead")

        url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0', '::1') else args.host}:{port}/"

        if args.check:
            print()
            print(f"  {ok('Environment is ready.')} Run `python run.py` to start.")
            print(f"  Would serve {url}")
            print()
            return 0

        print_banner(url, data_dir, provider_hint=not _providers_configured())

        dev_frontend = serve_dev_frontend(args.dev)
        if args.dev and dev_frontend is not None:
            print(f"  {dim('Vite: http://localhost:5173 (proxies the API to this backend)')}")
            time.sleep(0.8)

        if not args.no_browser:
            open_browser(url)

        try:
            return serve(python, args.host, port, reload=args.dev)
        finally:
            if dev_frontend is not None:
                dev_frontend.terminate()
    except LauncherError as exc:
        print()
        print(f"  {fail('Could not start RouteLLM')}")
        for line in str(exc).splitlines():
            print(f"  {line}")
        print()
        return 1
    except KeyboardInterrupt:
        print(f"\n  {dim('Stopped.')}\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
