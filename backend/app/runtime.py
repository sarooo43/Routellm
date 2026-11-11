"""Where the app is allowed to write, and how it detects its host.

Hugging Face Spaces run the container as a **non-root user** and the app
directory is effectively read-only, so anything that writes next to the source
(log files, SQLite fallbacks, ML caches) crashes at startup there. Rather than
sprinkle ``try/except`` around every write, the app asks this module for a
directory that is guaranteed writable.

Resolution order for :func:`writable_dir`:

1. ``ROUTELLM_RUNTIME_DIR`` — explicit override (tests, self-host).
2. ``/tmp`` when running on Hugging Face (``SPACE_ID`` set) or when
   ``ROUTELLM_READ_ONLY_APP_DIR=1`` is set (used by the read-only boot test).
3. ``<repo>/backend/.runtime`` for local development.

Detection is deliberately env-based (not "does /tmp exist"), so it is testable
and produces an explicit startup log line.
"""

import os
import tempfile
from pathlib import Path
from typing import Optional

#: Marker Hugging Face sets on every Space container.
HF_SPACE_ID_ENV = "SPACE_ID"
#: Explicit override used by tests and unusual hosts.
RUNTIME_DIR_ENV = "ROUTELLM_RUNTIME_DIR"
#: Explicit override for where local-first state (SQLite DB, generated Fernet
#: key) is stored. See :func:`data_dir`.
DATA_DIR_ENV = "ROUTELLM_DATA_DIR"
#: Force the read-only-filesystem behavior even when not on Hugging Face.
READ_ONLY_FLAG_ENV = "ROUTELLM_READ_ONLY_APP_DIR"

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
# NOTE: by module-history convention `_BACKEND_ROOT` actually points at the
# repository root (verified empirically), so the data dir lives at <repo>/data.



def data_dir() -> Path:
    """Return the per-user data directory for local-first persistence.

    Local-first storage rules:

    1. ``ROUTELLM_DATA_DIR`` — explicit override (run.py, Docker, tests).
    2. ``<repo>/data`` — the ordinary local default (gitignored; DB file and
       generated secrets live here).
    3. On a read-only app dir (Hugging Face, or the read-only test flag) the
       data falls back under the system temp dir, like :func:`writable_dir`.

    The directory is created if missing and contains state that must survive
    restarts: ``routellm.db`` (SQLite) and ``provider_key_encryption.key``.
    """
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        base = Path(override).expanduser()
        try:
            base.mkdir(parents=True, exist_ok=True)
            return base
        except OSError:
            pass  # unwritable override: fall through to the safe fallbacks
    if app_dir_is_expected_read_only():
        base = Path(tempfile.gettempdir()) / "routellm-data"
    else:
        base = _BACKEND_ROOT / "data"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        base = Path(tempfile.gettempdir()) / "routellm-data"
        base.mkdir(parents=True, exist_ok=True)
    return base


def running_on_huggingface() -> bool:
    """True when the process believes it is inside a Hugging Face Space."""
    return bool(os.environ.get(HF_SPACE_ID_ENV, "").strip())


def app_dir_is_expected_read_only() -> bool:
    """True when app-dir writes must be avoided (HF, or forced by the env flag)."""
    flag = os.environ.get(READ_ONLY_FLAG_ENV, "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    return running_on_huggingface()


def writable_dir(*parts: str, env_var: Optional[str] = None) -> Path:
    """Return a writable directory, creating it if needed.

    ``parts`` are appended to the base runtime directory (e.g.
    ``writable_dir("logs")``). ``env_var`` lets a caller point a specific
    feature somewhere else without touching this module.

    The path is *returned*, never written to beyond ``mkdir`` itself; callers
    decide whether they actually need the file.
    """
    override = os.environ.get(env_var, "").strip() if env_var else ""
    if override:
        base = Path(override).expanduser()
    else:
        explicit = os.environ.get(RUNTIME_DIR_ENV, "").strip()
        if explicit:
            base = Path(explicit).expanduser()
        elif app_dir_is_expected_read_only():
            base = Path(tempfile.gettempdir()) / "routellm"
        else:
            base = _BACKEND_ROOT / ".runtime"

    target = base.joinpath(*parts) if parts else base
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Even mkdir can fail (fully read-only container) — fall back to the
        # system temp dir, which is the one place both Docker and Spaces keep
        # writable, and never raise from a helper called during startup.
        target = Path(tempfile.gettempdir()).joinpath(*parts)
        target.mkdir(parents=True, exist_ok=True)
    return target


def describe_runtime_dir() -> str:
    """Human-readable runtime-dir description for the startup log."""
    try:
        return str(writable_dir())
    except Exception:
        return "<unavailable>"
