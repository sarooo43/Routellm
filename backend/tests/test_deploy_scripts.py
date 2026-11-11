"""Guards for the Hugging Face deploy scripts.

These tests exist because the deploy path is the one thing that cannot be
exercised from CI against the real target: it needs the owner's Hugging Face
credentials. Everything that CAN be checked statically is checked here, and the
push itself is replayed against a throwaway bare repository.

The bug this suite was written for: `git push hf <sha>:main --force` is
rejected by git 2.39+ with

    error: The destination you provided is not a full refname (i.e., starting
    with "refs/"). ... Neither worked, so we gave up.

because the source is a raw commit sha, so git cannot infer that "main" means a
branch — and a brand-new Space has no `main` ref to look up. The fix is the
fully qualified `<sha>:refs/heads/main`.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
PS1 = SCRIPTS / "deploy_hf.ps1"
SH = SCRIPTS / "deploy_hf.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-hf.yml"

#: Any push refspec in the deploy tooling must use the qualified branch form.
BAD_REFSPEC = re.compile(r":main\b(?!\s*$)")
GOOD_REFSPEC = "refs/heads/main"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Static contract for all three deploy entry points
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", [PS1, SH, WORKFLOW], ids=["powershell", "shell", "workflow"])
def test_push_uses_a_fully_qualified_destination(path):
    text = _read(path)
    assert GOOD_REFSPEC in text, f"{path.name} does not push to {GOOD_REFSPEC}"
    # The unqualified form must not survive in an actual git push line.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "push" not in stripped:
            continue
        assert '":main"' not in stripped and "':main'" not in stripped, (
            f"unqualified refspec in {path.name}: {stripped}"
        )
        assert 'main" --force' not in stripped or GOOD_REFSPEC in stripped, (
            f"unqualified refspec in {path.name}: {stripped}"
        )


@pytest.mark.parametrize("path", [PS1, SH, WORKFLOW], ids=["powershell", "shell", "workflow"])
def test_push_is_forced(path):
    """The Space history is rebuilt on every deploy, so the push must force."""
    text = _read(path)
    push_lines = [ln for ln in text.splitlines() if GOOD_REFSPEC in ln or "--force" in ln]
    assert any("--force" in ln for ln in push_lines), f"{path.name} does not force-push"


@pytest.mark.parametrize("path", [PS1, SH], ids=["powershell", "shell"])
def test_deploy_uses_subtree_split_of_backend(path):
    text = _read(path)
    assert "subtree split" in text
    assert "--prefix backend" in text


@pytest.mark.parametrize("path", [PS1, SH], ids=["powershell", "shell"])
def test_remote_url_has_no_embedded_credentials(path):
    """A token in a remote URL ends up in .git/config and in error output."""
    for line in _read(path).splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "huggingface.co/spaces" not in stripped:
            continue
        assert "@" not in stripped, f"credential-looking URL in {path.name}: {stripped}"
        assert "hf_" not in stripped, f"possible token in {path.name}: {stripped}"


#: A real Hugging Face token: "hf_" followed by a long opaque body. Deliberately
#: NOT a bare "hf_" check — "hf_app.py" legitimately appears in the comments.
HF_TOKEN_PATTERN = re.compile(r"hf_[A-Za-z0-9]{20,}")
#: Real DSN shapes that must never be baked into a script.
SECRET_PATTERNS = {
    "postgres password": re.compile(r"postgres(?:ql)?(?:\+asyncpg)?://[^\s:@]+:[^\s@]+@"),
    "redis password": re.compile(r"rediss?://[^\s:@]+:[^\s@]+@"),
    "neon password": re.compile(r"npg_[A-Za-z0-9]{6,}"),
    "upstash token": re.compile(r"gQAAAA[A-Za-z0-9]+"),
}


@pytest.mark.parametrize("path", [PS1, SH], ids=["powershell", "shell"])
def test_no_secrets_and_no_python_pin_side_effects(path):
    text = _read(path)
    # No hard-coded space id drift, no secrets, no node pin changes.
    assert "24.22.2" not in text
    assert "undici" not in text
    assert not HF_TOKEN_PATTERN.search(text), f"{path.name} contains an HF token"
    for label, pattern in SECRET_PATTERNS.items():
        assert not pattern.search(text), f"{path.name} contains a {label} with credentials"


# --------------------------------------------------------------------------
# PowerShell specifics (the NativeCommandError trap)
# --------------------------------------------------------------------------


def test_powershell_checks_last_exit_code_not_the_error_stream():
    """git prints progress on stderr; PS 5.1 turns that into a scary (harmless)
    NativeCommandError. The script must decide on $LASTEXITCODE only."""
    text = _read(PS1)
    assert "$LASTEXITCODE" in text
    # It must capture the push result before branching on it.
    assert re.search(r"\$pushExit\s*=\s*\$LASTEXITCODE", text)
    # And it must not use the error stream as the success signal.
    assert "$error[0]" not in text
    assert "trap {" not in text


# --------------------------------------------------------------------------
# Behavioural replay: does the documented git sequence actually work?
# --------------------------------------------------------------------------


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


@pytest.fixture()
def workspace(tmp_path):
    """A minimal repo shaped like RouteLLM (backend/ + scripts/), plus a bare
    'Space' to push into."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "backend").mkdir()
    (repo / "backend" / "hf_app.py").write_text("# entrypoint\n", encoding="utf-8")
    (repo / "backend" / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (repo / "README.md").write_text("root readme\n", encoding="utf-8")

    space = tmp_path / "space.git"
    for args in (
        ("init", "-q"),
        ("-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A"),
        ("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init"),
    ):
        result = _git(*args, cwd=repo)
        assert result.returncode == 0, result.stderr
    assert _git("init", "-q", "--bare", str(space), cwd=tmp_path).returncode == 0
    return repo, space


def test_qualified_refspec_push_succeeds(workspace):
    """The fixed form: subtree split, then push to refs/heads/main."""
    repo, space = workspace
    split = _git("subtree", "split", "--prefix", "backend", "HEAD", cwd=repo)
    if split.returncode != 0:  # pragma: no cover - git without subtree support
        pytest.skip(f"git subtree unavailable: {split.stderr.strip()}")
    sha = split.stdout.strip().splitlines()[-1]

    push = _git("push", str(space), f"{sha}:refs/heads/main", "--force", cwd=repo)
    assert push.returncode == 0, push.stderr

    # The Space root must now BE the backend folder — that is the whole point.
    listing = _git("--git-dir", str(space), "ls-tree", "--name-only", "refs/heads/main", cwd=repo)
    assert listing.returncode == 0, listing.stderr
    assert set(listing.stdout.split()) == {"hf_app.py", "requirements.txt"}


def test_unqualified_refspec_is_rejected(workspace):
    """Documents WHY the scripts use the qualified form (git >= 2.39)."""
    repo, space = workspace
    split = _git("subtree", "split", "--prefix", "backend", "HEAD", cwd=repo)
    if split.returncode != 0:  # pragma: no cover
        pytest.skip("git subtree unavailable")
    sha = split.stdout.strip().splitlines()[-1]

    push = _git("push", str(space), f"{sha}:main", "--force", cwd=repo)
    git_version = _git("--version", cwd=repo).stdout
    # Behaviour is version dependent; only assert where git is modern enough.
    if "2.3" in git_version or "2.4" in git_version:
        assert push.returncode != 0
        assert "full refname" in push.stderr.lower()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_shell_script_is_syntactically_valid():
    if sys.platform == "win32":
        # On Windows `bash` usually resolves to the WSL shim, whose
        # stdin/exec path fails without a real WLS distro installed.
        pytest.skip("bash on Windows resolves to the WSL shim (Linux/CI only)")
    result = subprocess.run(
        ["bash", "-n", str(SH)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
