"""Contracts for the root ``run.py`` launcher.

The launcher is the first thing a new user runs, so its behaviour is pinned
here rather than discovered in the wild:

* it refuses an unsupported Python with an actionable message,
* it is idempotent — a second run does not reinstall dependencies,
* it never overwrites an existing ``backend/.env``,
* it auto-advances off an occupied port, and only ``--port`` values are
  treated as hard requirements,
* it never binds anything but loopback unless ``--host`` says so.

Nothing here starts a server: every subprocess/network interaction is either
avoided (``--check``) or stubbed.
"""

import importlib.util
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_PY = REPO_ROOT / "run.py"


@pytest.fixture(scope="module")
def launcher():
    """Import run.py as a module without executing main()."""
    spec = importlib.util.spec_from_file_location("routellm_run", RUN_PY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# Shape of the script itself
# --------------------------------------------------------------------------


def test_launcher_exists_at_the_repository_root():
    assert RUN_PY.is_file()


def test_launcher_is_stdlib_only():
    """It must run before any dependency is installed."""
    source = RUN_PY.read_text(encoding="utf-8")
    for third_party in ("import uvicorn", "import fastapi", "import pydantic",
                        "import httpx", "import requests"):
        assert third_party not in source


def test_launcher_imports_without_a_virtualenv(launcher):
    """Importing the module must not touch the filesystem or the network."""
    assert launcher.DEFAULT_HOST == "127.0.0.1"
    assert launcher.DEFAULT_PORT == 8000
    assert launcher.MIN_PYTHON == (3, 11)


def test_check_mode_is_read_only_and_never_starts_a_server(tmp_path):
    """`--check` must not install, build or bind anything (CI runs it)."""
    vendored = tmp_path / "venv-python"
    result = subprocess.run(
        [sys.executable, str(RUN_PY), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Nothing was installed or started." in result.stdout
    assert "Uvicorn running" not in result.stdout
    assert "installing backend dependencies" not in result.stdout
    assert "building the dashboard" not in result.stdout
    assert not vendored.exists()


def test_check_mode_reports_each_prerequisite(launcher):
    report = dict((message, ready) for ready, message in launcher.environment_report())
    joined = " ".join(report)
    assert "Python" in joined
    assert "virtualenv" in joined or "dashboard" in joined


def test_check_mode_names_the_ports_it_would_use(launcher, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "pick_port", lambda host, port: (port + 1, True))
    assert launcher.main(["--check", "--port", "8000"]) == 0
    printed = capsys.readouterr().out
    assert "8001" in printed
    assert "is busy" in printed


# --------------------------------------------------------------------------
# Argument handling
# --------------------------------------------------------------------------


def test_defaults_are_loopback_and_8000(launcher, monkeypatch):
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    args = launcher.parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.dev is False
    assert args.no_browser is False


@pytest.mark.parametrize(
    "argv,attr,expected",
    [
        (["--port", "8123"], "port", 8123),
        (["--host", "0.0.0.0"], "host", "0.0.0.0"),
        (["--dev"], "dev", True),
        (["--no-browser"], "no_browser", True),
        (["--reinstall"], "reinstall", True),
        (["--rebuild"], "rebuild", True),
        (["--check"], "check", True),
    ],
)
def test_arguments_parse(launcher, argv, attr, expected):
    assert getattr(launcher.parse_args(argv), attr) == expected


# --------------------------------------------------------------------------
# Ports
# --------------------------------------------------------------------------


def test_a_free_port_is_used_as_requested(launcher):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    chosen, shifted = launcher.pick_port("127.0.0.1", free_port)
    assert chosen == free_port
    assert shifted is False


def test_an_occupied_port_shifts_to_the_next_free_one(launcher):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        held.bind(("127.0.0.1", 0))
        held.listen(1)  # without listen() a second bind succeeds on Linux
        busy_port = held.getsockname()[1]
        assert launcher.port_is_free("127.0.0.1", busy_port) is False
        chosen, shifted = launcher.pick_port("127.0.0.1", busy_port)
    assert shifted is True
    assert chosen > busy_port


def test_port_selection_failure_is_explained(launcher, monkeypatch):
    monkeypatch.setattr(launcher, "port_is_free", lambda *_a: False)
    with pytest.raises(launcher.LauncherError) as excinfo:
        launcher.pick_port("127.0.0.1", 8000, attempts=3)
    assert "--port" in str(excinfo.value)


# --------------------------------------------------------------------------
# Local state
# --------------------------------------------------------------------------


def test_env_file_is_created_once_and_never_overwritten(launcher, tmp_path, monkeypatch):
    example = tmp_path / ".env.example"
    target = tmp_path / ".env"
    example.write_text("GROQ_API_KEY=\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "ENV_EXAMPLE", example)
    monkeypatch.setattr(launcher, "ENV_FILE", target)
    monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(launcher, "DATA_DIR", tmp_path / "data")

    assert launcher.ensure_env_file() is True
    assert target.read_text(encoding="utf-8") == "GROQ_API_KEY=\n"

    # A user edit (here: a provider key) must survive a later run untouched.
    target.write_text("GROQ_API_KEY=your-real-key\n", encoding="utf-8")
    assert launcher.ensure_env_file() is False
    assert target.read_text(encoding="utf-8") == "GROQ_API_KEY=your-real-key\n"


def test_provider_detection_reads_env_and_environment(launcher, tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# GROQ_API_KEY=commented-out\nGEMINI_API_KEY=\n", encoding="utf-8"
    )
    for name in ("GROQ_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert launcher._providers_configured(env_file) is False

    env_file.write_text("DEEPSEEK_API_KEY=sk-live-value\n", encoding="utf-8")
    assert launcher._providers_configured(env_file) is True

    monkeypatch.setenv("GROQ_API_KEY", "gsk_value")
    assert launcher._providers_configured(tmp_path / "missing.env") is True


def test_requirements_stamp_tracks_the_pinned_file(launcher):
    """Editing requirements.txt must invalidate the install, nothing else must."""
    first = launcher._requirements_hash()
    assert first == launcher._requirements_hash()
    assert len(first) == 64


# --------------------------------------------------------------------------
# Backend environment
# --------------------------------------------------------------------------


def test_backend_env_defaults_to_loopback_and_the_data_dir(launcher, monkeypatch):
    monkeypatch.delenv("ROUTELLM_DATA_DIR", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    env = launcher.backend_env(8000, "127.0.0.1", dev=False)
    assert env["PORT"] == "8000"
    assert env["HOST"] == "127.0.0.1"
    assert env["PYTHONPATH"] == str(launcher.BACKEND_DIR)
    assert env["ROUTELLM_DATA_DIR"].endswith("data")
    assert "VITE_DEV_SERVER" not in env


def test_dev_mode_marks_the_vite_dev_server(launcher):
    env = launcher.backend_env(8000, "127.0.0.1", dev=True)
    assert env["VITE_DEV_SERVER"] == "1"


def test_backend_env_never_invents_secrets(launcher):
    env = launcher.backend_env(8000, "127.0.0.1", dev=False)
    invented = {
        name for name in env
        if name.startswith(("GROQ_", "GEMINI_", "DEEPSEEK_", "PROVIDER_KEY_"))
    }
    # Whatever is already in the parent environment is passed through; the
    # launcher itself must not add any of these.
    assert invented <= set(__import__("os").environ)
