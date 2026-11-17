"""The backend's build identity, reported by /health (T1 stale-build detection).

A Python source checkout has no compiled artifact, so its "build" IS the git
commit the process is running from. Reporting the short commit plus the commit
time lets the dashboard detect the classic stale-frontend problem: an old
``frontend/dist`` still served while the backend has moved on (exactly what the
owner hit after merging PR #16 — the old server and old bundle kept running).

Fail-soft by contract: every helper returns None when git is absent (a release
zip, a bare container, HF Spaces), and /health must never fail because of this.
Values are cached at first call — the commit of a running process cannot change
under it (restarting picks up a new commit; that is the point of the banner).
"""

from __future__ import annotations

import functools
import subprocess
from pathlib import Path

from app.utils.logger import get_logger

logger = get_logger(__name__)

#: The repository root: build_info.py lives at <repo>/backend/app/.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str) -> str | None:
    """Run one read-only git command; None on any failure (never raises)."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("build_info_git_unavailable", error=type(exc).__name__)
        return None
    output = result.stdout.strip()
    return output or None


@functools.lru_cache(maxsize=1)
def git_commit() -> str | None:
    """Short (7-char) commit this process runs from, e.g. ``35898f9``."""
    return _git("rev-parse", "--short", "HEAD")


@functools.lru_cache(maxsize=1)
def git_commit_time() -> str | None:
    """ISO-8601 committer date of HEAD (the honest 'build time' for source)."""
    return _git("log", "-1", "--format=%cI")


def build_info() -> dict[str, str | None]:
    """The /health ``build`` block. Never raises, never contains secrets."""
    return {
        "git_commit": git_commit(),
        "git_commit_time": git_commit_time(),
    }
