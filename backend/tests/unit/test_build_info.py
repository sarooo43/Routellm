"""T1 stale-build detection: the backend's build identity.

/health now reports ``build.git_commit`` + ``build.git_commit_time`` so the
dashboard can flag a frontend bundle built from a different commit (the exact
failure the owner hit: an old server and old bundle kept running after the
merge, so the walk and diagnostics looked broken).

Contract: fail-soft everywhere. A checkout without git (release zip, bare
container) must still serve /health — with None fields, never an error.
"""

import subprocess

from app.build_info import build_info, git_commit, git_commit_time


def test_the_commit_is_the_real_head_commit():
    """In a git checkout the reported commit IS HEAD (OBSERVED, not stubbed)."""
    expected = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert git_commit() == expected


def test_the_commit_time_is_iso8601():
    from datetime import datetime

    value = git_commit_time()
    assert value is not None
    # Parses as ISO-8601 with a timezone (git %cI), e.g. 2026-11-15T04:05:23+05:00.
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None


def test_build_info_contains_both_fields_and_nothing_secret():
    info = build_info()
    assert set(info) == {"git_commit", "git_commit_time"}
    # A commit hash and an ISO date — never more (no paths, no keys, no DSNs).
    assert info["git_commit"] and len(str(info["git_commit"])) <= 40
    assert "\n" not in str(info["git_commit"])


def test_every_helper_is_fail_soft_when_git_is_absent(monkeypatch):
    """No git binary (or a non-repo cwd) must read as None, never raise."""

    def explode(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "run", explode)
    git_commit.cache_clear()
    git_commit_time.cache_clear()
    try:
        assert build_info() == {"git_commit": None, "git_commit_time": None}
    finally:
        git_commit.cache_clear()
        git_commit_time.cache_clear()


def test_an_empty_git_answer_is_none_not_an_empty_string(monkeypatch):
    """A git that exits 0 but prints nothing must not yield '' as a commit."""

    class FakeResult:
        stdout = "\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeResult())
    git_commit.cache_clear()
    git_commit_time.cache_clear()
    try:
        assert git_commit() is None
    finally:
        git_commit.cache_clear()
        git_commit_time.cache_clear()
