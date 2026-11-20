"""C2: the live eval as a REAL subprocess against a REAL uvicorn server.

Every other eval test loads the script with importlib and fakes the HTTP
client. That is how two shipped bugs survived a green suite: a fake returned
200 where the server sends 201, and a fake answered a provider-keys call with
no credential. This file runs the thing a user actually runs:

    python evals/run_resilience_eval.py --live --dry-run --yes-spend-quota

with ``sys.executable``, a real uvicorn on an OS-chosen free port, a temp data
dir and Chaos Mode on, so stdout, the exit code, the revocation and the absence
of /route calls are all observed rather than assumed.

No secrets: provider keys are fakes, and only prefixes are ever printed.
"""

import json
import os
import socket
import subprocess
import signal
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
EVAL_SCRIPT = REPO_ROOT / "evals" / "run_resilience_eval.py"
FAKE_KEYS = {
    "gemini": "AIzaFAKE000000000000000000000000000",
    "groq": "gsk_FAKE000000000000000000000000000000",
    "deepseek": "sk-FAKE000000000000000000000000000000",
}


def _free_port() -> int:
    """An OS-chosen port. Never a hardcoded one: CI runs tests in parallel."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(base_url: str, proc: subprocess.Popen, timeout: float = 60.0) -> None:
    """Bounded readiness polling: fail fast, never hang the suite."""
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"backend exited before becoming ready (code {proc.returncode})"
            )
        try:
            if httpx.get(f"{base_url}/health", timeout=1.0).status_code == 200:
                return
        except Exception:
            time.sleep(0.15)
    raise AssertionError(f"backend not ready within {timeout}s at {base_url}")


@pytest.fixture
def backend(tmp_path):
    """A real uvicorn process: temp SQLite, temp data dir, Chaos Mode on."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    env = dict(os.environ)
    env.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'live.db'}",
            "ROUTTENER_DATA_DIR": str(data_dir),
            "REDIS_URL": "",
            "DEMO_MODE_ENABLED": "true",
            "CHAOS_MODE_ENABLED": "true",
            "REQUIRE_API_KEY": "false",
            "ALLOWED_HOSTS": "*",
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "PYTHONPATH": str(REPO_ROOT / "backend"),
        }
    )
    # A key in the ambient environment would silently change the outcome.
    for name in ("ROUTTENER_EVAL_API_KEY", "ROUTELLM_EVAL_API_KEY"):
        env.pop(name, None)

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=REPO_ROOT / "backend",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_ready(base_url, proc)
        yield {
            "base_url": base_url,
            "env": env,
            "data_dir": data_dir,
            "port": port,
            "db_path": tmp_path / "live.db",
        }
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
            proc.kill()
            proc.communicate()


def _http(base_url):
    import httpx

    return httpx.Client(base_url=base_url, timeout=20.0)


def _mint_key(base_url: str) -> str:
    with _http(base_url) as c:
        resp = c.post("/api/v1/keys/generate", json={"label": "owner"})
        assert resp.status_code == 201, resp.text
        return resp.json()["api_key"]


def _save_providers(base_url: str, owner_key: str, providers) -> None:
    """Save through the REAL endpoint the dashboard calls."""
    with _http(base_url) as c:
        for name in providers:
            resp = c.put(
                f"/api/v1/provider-keys/{name}",
                json={"api_key": FAKE_KEYS[name]},
                headers={"X-API-Key": owner_key},
            )
            assert resp.status_code == 200, resp.text


def _run_eval(backend, extra_env=None, dry_run=True) -> subprocess.CompletedProcess:
    env = dict(backend["env"])
    env.update(extra_env or {})
    args = [sys.executable, str(EVAL_SCRIPT), "--live",
            "--base-url", backend["base_url"], "--n", "3"]
    if dry_run:
        args.append("--dry-run")
    args.append("--yes-spend-quota")
    return subprocess.run(
        args, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300
    )


# ---------------------------------------------------------------------------
# The four cases the brief names
# ---------------------------------------------------------------------------

def test_zero_providers_saved_aborts_with_the_full_report(backend):
    result = _run_eval(backend)
    # The gate report is stdout; the FATAL blocker is stderr. A user sees both,
    # so the assertions read both.
    both = result.stdout + result.stderr

    assert result.returncode != 0, "a run with no providers must not exit 0"
    assert "Providers detected: none (0 with saved keys)" in result.stdout
    assert "Chaos check: enabled" in result.stdout
    assert "Request budget:" in result.stdout
    assert "DRY RUN" in result.stdout
    assert "fewer than 2 providers" in both
    assert "FATAL" in both
    # The recovery instructions are the only thing a user can act on.
    assert "not visible to this key" in both
    assert "ROUTTENER_EVAL_API_KEY" in both


def test_one_provider_saved_still_aborts(backend):
    owner = _mint_key(backend["base_url"])
    _save_providers(backend["base_url"], owner, ["groq"])

    result = _run_eval(backend, {"ROUTTENER_EVAL_API_KEY": owner})
    both = result.stdout + result.stderr

    assert result.returncode != 0
    assert "Providers detected: groq (1 with saved keys)" in both
    assert "fewer than 2 providers" in both
    # Still a dry run: it refused BEFORE spending anything.
    assert "DRY RUN" in result.stdout


def test_two_providers_saved_passes_and_sends_no_route_calls(backend):
    owner = _mint_key(backend["base_url"])
    _save_providers(backend["base_url"], owner, ["groq", "deepseek"])

    before = _task_log_count(backend["db_path"])
    result = _run_eval(backend, {"ROUTTENER_EVAL_API_KEY": owner})
    after = _task_log_count(backend["db_path"])

    assert result.returncode == 0, result.stdout + result.stderr
    # Server-side witness, not the script's own promise: every routed request
    # writes exactly one task_logs row (app/core/executor.py), so an unchanged
    # count means no /route call reached the app.
    assert after == before, f"dry run wrote {after - before} task_logs rows"
    out = result.stdout
    assert "Providers detected: groq, deepseek (2 with saved keys)" in out
    assert "DRY RUN: no /api/v1/route call was sent" in out
    # A dry run must not fabricate a measurement table.
    assert "| provider_down | router |" not in out
    # It must not invent a key it did not need.
    assert "minted" not in out
    assert "reused from $ROUTTENER_EVAL_API_KEY" in out


def test_a_user_supplied_key_is_never_revoked(backend):
    """The owner's own key is not ours to delete."""
    owner = _mint_key(backend["base_url"])
    _save_providers(backend["base_url"], owner, ["groq", "deepseek"])

    result = _run_eval(backend, {"ROUTTENER_EVAL_API_KEY": owner})
    assert result.returncode == 0

    with _http(backend["base_url"]) as c:
        still_there = c.get("/api/v1/keys/me", headers={"X-API-Key": owner})
    assert still_there.status_code == 200, (
        "the eval revoked a key it did not mint"
    )


def test_chaos_disabled_aborts_before_spending_quota(backend, tmp_path):
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = dict(backend["env"])
    env["CHAOS_MODE_ENABLED"] = "false"
    env["PORT"] = str(port)
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'noc.db'}"

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=REPO_ROOT / "backend", env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        _wait_ready(base_url, proc)
        owner = _mint_key(base_url)
        _save_providers(base_url, owner, ["groq", "deepseek"])
        run_env = dict(env)
        run_env["ROUTTENER_EVAL_API_KEY"] = owner
        result = subprocess.run(
            [sys.executable, str(EVAL_SCRIPT), "--live", "--base-url", base_url,
             "--n", "3", "--dry-run", "--yes-spend-quota"],
            cwd=REPO_ROOT, env=run_env, capture_output=True, text=True, timeout=300,
        )
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            proc.communicate()

    both = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Chaos check: NOT enabled" in both
    assert "CHAOS_MODE_ENABLED" in both
    assert "DRY RUN" in both


def _start_enforced_server(backend_env, db_path, tmp_path):
    """A server that ENFORCES auth, so a revoked key is observably a 401."""
    port = _free_port()
    env = dict(backend_env)
    env.update(
        {
            "PORT": str(port),
            "REQUIRE_API_KEY": "true",
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=REPO_ROOT / "backend", env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    _wait_ready(base_url, proc)
    return proc, base_url, env


def _task_log_count(db_path: Path) -> int:
    """Rows the router writes for every executed request."""
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        try:
            return conn.execute("SELECT COUNT(*) FROM task_logs").fetchone()[0]
        except sqlite3.OperationalError:
            return 0


def _active_key_count(db_path: Path) -> int:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]


def test_a_minted_key_is_revoked_after_a_blocked_run(backend, tmp_path):
    """0 providers -> the run mints its own key, gets blocked, and must revoke.

    On a server that enforces auth the revocation is observable: the minted
    key stops authenticating. Without that, "revoked" is only a word in stdout.
    """
    import httpx

    db_path = tmp_path / "enforced.db"
    proc, base_url, env = _start_enforced_server(backend["env"], db_path, tmp_path)
    try:
        assert _active_key_count(db_path) == 0
        result = subprocess.run(
            [sys.executable, str(EVAL_SCRIPT), "--live", "--base-url", base_url,
             "--n", "3", "--dry-run", "--yes-spend-quota"],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300,
        )
        both = result.stdout + result.stderr

        assert result.returncode != 0
        assert "API key: revoked" in both
        # It minted one key (there were none), and that key is now inactive.
        assert _active_key_count(db_path) == 1
        with httpx.Client(base_url=base_url, timeout=20.0) as c:
            # Proof that auth is actually enforced on this server: a key that
            # does not exist is refused. (Without this, "revoked" could be a
            # word in a log on a server that lets everything through.)
            rows = c.get("/api/v1/keys/me", headers={"X-API-Key": "rk_probe"})
        assert rows.status_code == 401
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            proc.communicate()

    import sqlite3

    with sqlite3.connect(db_path) as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM api_keys WHERE is_active = 1"
        ).fetchone()[0]
    assert active == 0, "the eval left an active key behind"


def test_ctrl_c_reports_one_sentence_instead_of_a_traceback(tmp_path):
    """Interrupt a live eval only once its HTTP request is actually blocked.

    Background process managers may inherit SIGINT as SIG_IGN. Start the child
    with the normal interactive Python SIGINT handler, then wait for the stub
    to accept its request: asyncio has installed its handler by that point.
    """
    import threading

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(0.1)
    port = listener.getsockname()[1]
    accepted = threading.Event()
    stop = threading.Event()
    events = []

    def _accept_and_stall() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
                break
            except socket.timeout:
                continue
            except OSError as exc:
                events.append(f"accept failed: {exc}")
                return
        else:
            return
        with conn:
            events.append("accepted connection; holding it open")
            accepted.set()
            stop.wait(45)
        events.append("connection closed")

    thread = threading.Thread(target=_accept_and_stall, daemon=True)
    thread.start()
    env = dict(os.environ)
    for name in ("ROUTTENER_EVAL_API_KEY", "ROUTELLM_EVAL_API_KEY"):
        env.pop(name, None)
    # A fresh interpreter avoids preexec_fn in a multithreaded process. The
    # exec leaves the normal Python handler in place for asyncio.run to replace.
    bootstrap = (
        "import runpy, signal, sys; "
        "signal.signal(signal.SIGINT, signal.default_int_handler); "
        "path = sys.argv.pop(1); "
        "runpy.run_path(path, run_name='__main__')"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", bootstrap, str(EVAL_SCRIPT), "--live",
         "--yes-spend-quota", "--base-url", f"http://127.0.0.1:{port}"],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    out = err = ""
    try:
        deadline = time.monotonic() + 30
        while not accepted.wait(0.1):
            if proc.poll() is not None or time.monotonic() >= deadline:
                break
        if accepted.is_set() and proc.poll() is None:
            proc.send_signal(signal.SIGINT)
        else:
            proc.kill()
        try:
            out, err = proc.communicate(timeout=40)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            pytest.fail(f"child did not exit after SIGINT; stdout={out!r}; stderr={err!r}; stub={events!r}")
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=2)
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    diagnostic = f"stdout={out!r}; stderr={err!r}; stub={events!r}"
    assert accepted.is_set(), f"child never reached the stub; {diagnostic}"
    assert "KeyboardInterrupt" not in out + err, diagnostic
    assert proc.returncode == 130, f"exit={proc.returncode}; {diagnostic}"
    assert "Interrupted." in out + err, diagnostic

@pytest.mark.parametrize("value", ["   ", "echo ${X:0:8}", "rk_bad key", "rk_bad\nkey"])
def test_malformed_eval_key_never_mints_or_echoes(backend, value):
    before = _active_key_count(backend["db_path"])
    result = _run_eval(backend, {"ROUTTENER_EVAL_API_KEY": value})
    assert result.returncode != 0
    assert "FATAL: the supplied eval API key is malformed" in result.stderr
    assert value not in result.stdout + result.stderr
    assert _active_key_count(backend["db_path"]) == before


def test_empty_eval_key_announces_before_detection(backend):
    result = _run_eval(backend, {"ROUTTENER_EVAL_API_KEY": ""})
    assert result.returncode != 0
    assert result.stdout.index("minting a throwaway key") < result.stdout.index("Providers detected:")
    assert "API key: revoked" in result.stdout


def test_prompt_requires_terminal(backend):
    result = subprocess.run(
        [sys.executable, str(EVAL_SCRIPT), "--live", "--dry-run", "--yes-spend-quota",
         "--base-url", backend["base_url"], "--api-key-prompt"],
        cwd=REPO_ROOT, env=backend["env"], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert "requires an interactive terminal" in result.stderr


def test_prompt_takes_precedence_and_never_echoes_key(backend):
    import pty
    import select

    owner = _mint_key(backend["base_url"])
    _save_providers(backend["base_url"], owner, ["groq", "deepseek"])
    env = dict(backend["env"], ROUTTENER_EVAL_API_KEY="bad env", ROUTELLM_EVAL_API_KEY="also bad")
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, str(EVAL_SCRIPT), "--live", "--dry-run", "--yes-spend-quota",
         "--base-url", backend["base_url"], "--api-key-prompt"],
        cwd=REPO_ROOT, env=env, stdin=slave, stdout=slave, stderr=slave, close_fds=True,
    )
    os.close(slave)
    data = b""
    try:
        deadline = time.monotonic() + 30
        while b"Routtener API key:" not in data and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                data += os.read(master, 4096)
            if proc.poll() is not None:
                break
        assert b"Routtener API key:" in data
        os.write(master, owner.encode() + b"\n")
        deadline = time.monotonic() + 30
        while proc.poll() is None and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    data += os.read(master, 4096)
                except OSError:
                    break
        proc.wait(timeout=10)
        try:
            while select.select([master], [], [], 0)[0]:
                data += os.read(master, 4096)
        except OSError:
            pass
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)
    assert proc.returncode == 0
    assert b"Providers detected: groq, deepseek" in data
    assert owner.encode() not in data
    with _http(backend["base_url"]) as c:
        assert c.get("/api/v1/keys/me", headers={"X-API-Key": owner}).status_code == 200


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000.", "127.0.0.1:8000", "http://:8000", "http://127.0.0.1:abc"])
@pytest.mark.parametrize("script", [EVAL_SCRIPT, REPO_ROOT / "benchmark" / "run_benchmark.py"])
def test_invalid_base_url_fails_before_minting(backend, url, script):
    before = _active_key_count(backend["db_path"])
    args = [sys.executable, str(script), "--base-url", url]
    if script == EVAL_SCRIPT:
        args += ["--live", "--dry-run", "--yes-spend-quota"]
    result = subprocess.run(args, cwd=REPO_ROOT, env=backend["env"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert result.stderr.startswith("FATAL: --base-url is not a valid URL")
    assert len(result.stderr.splitlines()) == 1
    assert "Traceback" not in result.stderr
    assert _active_key_count(backend["db_path"]) == before
