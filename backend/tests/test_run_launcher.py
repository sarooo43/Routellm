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


def test_an_impossible_port_is_rejected_with_one_clean_line(launcher, capsys):
    """OBSERVED: `--port 99999` escaped as a raw OverflowError traceback.

    Port 65536 is out of range, so socket.bind raises OverflowError, which is
    NOT an OSError and sailed past port_is_free()'s except clause. The user saw
    a stack trace instead of a sentence.
    """
    with pytest.raises(SystemExit) as excinfo:
        launcher.main(["--check", "--port", "99999"])
    assert excinfo.value.code == 2  # argparse usage error, not a crash
    stderr = capsys.readouterr().err
    assert "port must be between 0 and 65535" in stderr
    assert "Traceback" not in stderr
    assert "OverflowError" not in stderr


def test_a_negative_port_is_rejected_with_one_clean_line(launcher, capsys):
    with pytest.raises(SystemExit) as excinfo:
        launcher.main(["--check", "--port", "-5"])
    assert excinfo.value.code == 2
    assert "port must be between 0 and 65535" in capsys.readouterr().err


def test_the_port_parser_still_accepts_real_values(launcher):
    assert launcher._port_number("8000") == 8000
    assert launcher._port_number("0") == 0


def test_a_free_port_is_used_as_requested(launcher):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    chosen, shifted = launcher.pick_port("127.0.0.1", free_port)
    assert chosen == free_port
    assert shifted is False


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows SO_REUSEADDR lets port_is_free's probe bind a port an "
        "active listener holds, so the occupied-port detection is "
        "Linux-side behaviour; the launcher runs on Linux in production"
    ),
)
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


def test_provider_detection_counts_keys_saved_by_the_dashboard(
    launcher, tmp_path, monkeypatch
):
    """C4(c): the banner said "No provider key configured yet" while three
    keys were saved in the database.

    A key can live in the environment, in .env, or in the SQLite database the
    dashboard writes. Anything else makes the banner lie to the user.
    """
    import sqlite3

    for name in ("GROQ_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("# nothing set\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # No database at all -> not configured.
    assert launcher._providers_configured(env_file, data_dir) is False

    db_path = data_dir / "routellm.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE provider_keys ("
            " id INTEGER PRIMARY KEY, api_key_id INTEGER, provider_name TEXT,"
            " encrypted_key TEXT NOT NULL)"
        )
        conn.commit()

    # Table exists but empty -> still not configured.
    assert launcher._providers_configured(env_file, data_dir) is False

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO provider_keys (api_key_id, provider_name, encrypted_key)"
            " VALUES (1, 'gemini', 'gAAAAA-fake-ciphertext')"
        )
        conn.commit()

    # A dashboard-saved key counts, with no env var and nothing in .env.
    assert launcher._providers_configured(env_file, data_dir) is True


def test_provider_detection_never_breaks_startup_on_a_bad_database(
    launcher, tmp_path, monkeypatch
):
    """A corrupt/unreadable DB must not crash the banner - worst case it says
    "not configured", never raises."""
    for name in ("GROQ_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "routellm.db").write_bytes(b"this is not a sqlite database")
    assert launcher._providers_configured(tmp_path / "none.env", data_dir) is False


@pytest.mark.parametrize(
    "version,too_old",
    [
        (None, False),      # no Node at all: a different message, not "too old"
        ((20, 18), True),
        ((22, 17), True),
        ((22, 18), True),   # undici 8 needs >=22.19.0, exactly
        ((22, 19), False),
        ((24, 0), False),
    ],
)
def test_node_too_old_matches_the_build_toolchain(launcher, version, too_old):
    """README used to promise Node 20+, which vite 8 / vitest 5 / jsdom 30 refuse."""
    assert launcher.node_too_old(version) is too_old


def test_package_json_declares_the_engine_the_ci_comment_points_at(launcher):
    """CI says "see frontend/package.json engines" - so it must exist."""
    import json
    from pathlib import Path

    root = Path(launcher.__file__).resolve().parent
    data = json.loads((root / "frontend" / "package.json").read_text())
    assert data["engines"]["node"] == ">=22.19.0"


# --------------------------------------------------------------------------
# C4(a): the Codespaces hint must print a URL that can actually be opened
# --------------------------------------------------------------------------


def test_codespace_hint_includes_the_port(launcher, monkeypatch, capsys, tmp_path):
    """OBSERVED before the fix: "https://myspace-.app.github.dev" - no port.

    GitHub forwards as <codespace>-<port>.<domain>; a URL without the port is
    unopenable and looks like the codespace is broken.
    """
    monkeypatch.setenv("CODESPACE_NAME", "myspace")
    monkeypatch.setenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")

    launcher.print_banner("http://127.0.0.1:8123/", tmp_path, provider_hint=False)
    out = capsys.readouterr().out

    assert "https://myspace-8123.app.github.dev" in out
    assert "myspace-.app.github.dev" not in out


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://127.0.0.1:8123/", 8123),
        ("http://127.0.0.1:8000", 8000),
        ("https://example.test/", 443),
        ("http://localhost/", 8000),
    ],
)
def test_port_of(launcher, url, expected):
    assert launcher.port_of(url) == expected


def test_requirements_stamp_tracks_the_pinned_file(launcher):
    """Editing requirements.txt must invalidate the install, nothing else must."""
    first = launcher._requirements_hash()
    assert first == launcher._requirements_hash()
    assert len(first) == 64


# --------------------------------------------------------------------------
# Backend environment
# --------------------------------------------------------------------------


def test_backend_env_defaults_to_loopback_and_the_data_dir(launcher, monkeypatch):
    monkeypatch.delenv("ROUTTENER_DATA_DIR", raising=False)
    monkeypatch.delenv("ROUTELLM_DATA_DIR", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    env = launcher.backend_env(8000, "127.0.0.1", dev=False)
    assert env["PORT"] == "8000"
    assert env["HOST"] == "127.0.0.1"
    assert env["PYTHONPATH"] == str(launcher.BACKEND_DIR)
    assert env["ROUTTENER_DATA_DIR"].endswith("data")
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


# --------------------------------------------------------------------------
# Stale-build detection (T1): sources newer than dist must trigger a rebuild
# --------------------------------------------------------------------------


def _make_frontend(tmp_path, monkeypatch, launcher):
    """A fake frontend tree: one source file and one built bundle."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    source = src_dir / "App.tsx"
    source.write_text("export const x = 1\n", encoding="utf-8")
    dist = tmp_path / "dist"
    dist.mkdir()
    bundle = dist / "index.html"
    bundle.write_text("<html></html>\n", encoding="utf-8")
    # Bundling dist/index.html writes AFTER the source, as a real build does:
    # this base state is "fresh". Individual tests then age one side.
    bundle_time = source.stat().st_mtime + 10
    import os

    os.utime(bundle, (bundle_time, bundle_time))
    monkeypatch.setattr(launcher, "FRONTEND_DIR", tmp_path)
    monkeypatch.setattr(launcher, "DIST_DIR", dist)
    return source, bundle


def test_a_fresh_build_is_not_stale(launcher, tmp_path, monkeypatch):
    _make_frontend(tmp_path, monkeypatch, launcher)
    assert launcher.frontend_is_stale() is False


def test_a_source_newer_than_the_bundle_is_stale(launcher, tmp_path, monkeypatch):
    import os

    source, _bundle = _make_frontend(tmp_path, monkeypatch, launcher)
    future = source.stat().st_mtime + 100
    os.utime(source, (future, future))
    assert launcher.frontend_is_stale() is True


def test_a_nested_source_file_counts_too(launcher, tmp_path, monkeypatch):
    import os

    _source, _bundle = _make_frontend(tmp_path, monkeypatch, launcher)
    nested = tmp_path / "src" / "deep" / "edit.tsx"
    nested.parent.mkdir()
    nested.write_text("export const y = 2\n", encoding="utf-8")
    bundle_mtime = (tmp_path / "dist" / "index.html").stat().st_mtime
    os.utime(nested, (bundle_mtime + 5, bundle_mtime + 5))
    assert launcher.frontend_is_stale() is True


def test_a_missing_bundle_is_reported_missing_not_stale(launcher, tmp_path, monkeypatch):
    _source, _bundle = _make_frontend(tmp_path, monkeypatch, launcher)
    (tmp_path / "dist" / "index.html").unlink()
    assert launcher.frontend_is_stale() is False


def test_an_untouchable_source_reads_as_not_stale(launcher, tmp_path, monkeypatch):
    """Fail-soft: a stat error must not loop the builder (T1 contract)."""
    import pathlib

    _source, _bundle = _make_frontend(tmp_path, monkeypatch, launcher)
    monkeypatch.setattr(
        pathlib.Path,
        "stat",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("locked")),
    )
    assert launcher.frontend_is_stale() is False


def test_ensure_frontend_rebuilds_when_stale(launcher, tmp_path, monkeypatch, capsys):
    """A stale dist must not survive a restart just because it exists."""
    import os

    source, _bundle = _make_frontend(tmp_path, monkeypatch, launcher)
    future = source.stat().st_mtime + 100
    os.utime(source, (future, future))

    monkeypatch.setattr(launcher, "dist_is_usable", lambda: True)
    # 22.19+, not 22.0: vite 8 refuses anything below and the check must agree.
    monkeypatch.setattr(launcher, "node_version", lambda: (22, 19, 0))
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "npm")
    calls = []
    monkeypatch.setattr(
        launcher, "_run", lambda cmd, what, cwd: calls.append(cmd)
    )

    result = launcher.ensure_frontend()
    assert result == tmp_path / "dist"
    # node_modules is absent in the fake tree, so ci runs first; the build
    # itself must be among the calls (that is the stale-rebuild proof).
    assert any("build" in call[-1] for call in calls)
    assert "rebuilding" in capsys.readouterr().out


def test_ensure_frontend_skips_the_rebuild_when_fresh(launcher, tmp_path, monkeypatch):
    _make_frontend(tmp_path, monkeypatch, launcher)
    monkeypatch.setattr(launcher, "dist_is_usable", lambda: True)
    calls = []
    monkeypatch.setattr(launcher, "_run", lambda cmd, what, cwd: calls.append(cmd))
    result = launcher.ensure_frontend()
    assert result == tmp_path / "dist"
    assert calls == []
