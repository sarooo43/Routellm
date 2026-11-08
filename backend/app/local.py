"""Local-first runtime: data directory, SQLite default, and the BYOK key file.

The open-source, run-it-on-your-laptop mode has to work with **no accounts, no
Postgres, no Redis and no configuration file**. That means three things must be
derived automatically rather than asked for:

* a writable **data directory** (SQLite file, generated encryption key, logs),
* a **database URL** (SQLite in that directory, unless ``DATABASE_URL`` says
  otherwise),
* an **encryption key** for BYOK, generated once and reused.

Design rules this module follows:

* **Import-safe.** Nothing is created at import time; callers ask explicitly.
  ``import app.local`` must never touch the filesystem.
* **Never log a secret.** The encryption key is written to disk and never
  returned in a log line, an exception message, or an HTTP response. Only its
  *path* and a boolean "is it configured" may appear in output.
* **Never assume POSIX.** File permissions are tightened with ``chmod`` only
  where the OS supports it (``os.name == "posix"``), and a failure to tighten
  them is logged, never fatal.
* **Explicit beats implicit.** Every directory and file can be overridden
  (``ROUTELLM_DATA_DIR``); the default is ``./data`` next to the repo root so a
  contributor can see and delete their own state.

Resolution order for the data directory:

1. ``ROUTELLM_DATA_DIR`` (absolute or relative to the current directory).
2. ``<repo>/data`` where the repo root is the parent of ``backend/``.
3. The OS user-data directory, when the repo checkout is read-only
   (for example a prebuilt release unzipped into ``/opt`` or ``Program Files``).
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Override for the data directory.
DATA_DIR_ENV = "ROUTELLM_DATA_DIR"
#: Filename of the SQLite database inside the data directory.
SQLITE_FILENAME = "routellm.db"
#: Filename of the generated Fernet key inside the data directory.
ENCRYPTION_KEY_FILENAME = "encryption.key"
#: ``<repo>`` = the parent of the ``backend`` package directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Directory names that must never be committed; used by ``.gitignore`` docs.
_GITIGNORE_ENTRIES = ("data/", ".venv/", ".env")


def _user_data_dir(app_name: str = "routellm") -> Path:
    """Best-effort per-user data directory for a read-only checkout.

    Follows the platform convention rather than inventing one:

    * Windows: ``%LOCALAPPDATA%\\<app>``
    * macOS: ``~/Library/Application Support/<app>``
    * Linux/other: ``$XDG_DATA_HOME/<app>`` or ``~/.local/share/<app>``
    """
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / app_name
    elif sys.platform == "darwin":  # pragma: no cover - macOS only
        return Path.home() / "Library" / "Application Support" / app_name
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / app_name
    return Path.home() / ".local" / "share" / app_name


def _is_writable(directory: Path) -> bool:
    """True when ``directory`` exists (or can be created) and is writable."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".routellm-write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def data_dir(create: bool = True) -> Path:
    """Return the local data directory, creating it when asked.

    Never raises for a read-only checkout: it falls through to the per-user
    directory instead of crashing the first run.
    """
    # The env var wins (tests and the launcher set it), then the pydantic
    # setting, so both `ROUTELLM_DATA_DIR=... python run.py` and a .env entry
    # work without the caller knowing which one to set.
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if not override:
        try:
            from app.config import settings

            override = (settings.ROUTELLM_DATA_DIR or "").strip()
        except Exception:  # pragma: no cover - settings import cannot fail
            override = ""
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        if create:
            candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    repo_candidate = _REPO_ROOT / "data"
    if create:
        if _is_writable(repo_candidate):
            return repo_candidate
        fallback = _user_data_dir()
        fallback.mkdir(parents=True, exist_ok=True)
        logger.info(
            "data_dir_fallback_user_dir",
            reason="repository directory is not writable",
            path=str(fallback),
        )
        return fallback
    return repo_candidate if repo_candidate.exists() else _user_data_dir()


def restrict_permissions(path: Path) -> bool:
    """Tighten a file to owner-only read/write where the OS supports it.

    Returns True when permissions were applied. On Windows this is a no-op
    (POSIX mode bits are not meaningful there) and returns False; the caller
    must not treat that as an error.
    """
    if os.name != "posix":  # pragma: no cover - Windows
        return False
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        return True
    except OSError as exc:  # pragma: no cover - exotic filesystems
        logger.warning(
            "permissions_not_tightened",
            path=str(path),
            error=str(exc)[:200],
        )
        return False


def default_database_url() -> str:
    """The SQLite URL used when ``DATABASE_URL`` is not set.

    Absolute path so the app behaves the same from any working directory, and
    so the ``sqlite+aiosqlite:///`` form is never ambiguous.
    """
    db_path = data_dir(create=True) / SQLITE_FILENAME
    # Three slashes + absolute path == four in the string ("sqlite+aiosqlite://" + "/abs").
    return f"sqlite+aiosqlite:///{db_path}"


def resolve_database_url(configured: Optional[str]) -> str:
    """Return the effective database URL.

    An explicit ``DATABASE_URL`` (Postgres or SQLite) always wins; local-first
    only supplies a default when nothing was configured.
    """
    value = (configured or "").strip()
    if value:
        return value
    url = default_database_url()
    logger.info("database_defaulted_to_sqlite", url="sqlite+aiosqlite:///<data-dir>/routellm.db")
    return url


def encryption_key_path() -> Path:
    """Where the generated Fernet key lives (inside the data directory)."""
    return data_dir(create=True) / ENCRYPTION_KEY_FILENAME


def load_or_create_encryption_key(configured: Optional[str] = None) -> Optional[str]:
    """Return the encryption key, generating and persisting one on first run.

    Precedence:

    1. ``configured`` (the ``PROVIDER_KEY_ENCRYPTION_KEY`` setting) — used
       verbatim, never written to disk.
    2. An existing ``<data dir>/encryption.key`` — read back as-is.
    3. Otherwise a new Fernet key is generated, written with ``0o600`` where
       supported, and returned.

    Returns ``None`` only when generation is impossible (no writable directory,
    or ``cryptography`` missing). The caller then keeps today's behaviour of
    failing closed with a clear message.

    SECURITY: the key itself is never logged. Only the path and whether it was
    reused or created.
    """
    if configured and configured.strip():
        return configured.strip()

    path = encryption_key_path()
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                logger.info("encryption_key_reused", path=str(path))
                return existing
    except OSError as exc:
        logger.warning("encryption_key_unreadable", path=str(path), error=str(exc)[:200])

    try:
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode("utf-8")
    except Exception as exc:  # pragma: no cover - cryptography is a hard dep
        logger.warning("encryption_key_generation_failed", error=str(exc)[:200])
        return None

    try:
        # Create with 0o600 from the start where the OS honours it, so the key
        # is never briefly world-readable.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(key)
        restrict_permissions(path)
    except OSError as exc:
        logger.error("encryption_key_persist_failed", path=str(path), error=str(exc)[:200])
        # Still usable in-memory for this run; it just will not survive a
        # restart, which the caller surfaces in the startup banner.
        return key

    logger.info("encryption_key_created", path=str(path))
    return key


def describe_data_location() -> str:
    """Human-readable, secret-free description for the startup banner."""
    directory = data_dir(create=False)
    return f"{directory} (sqlite: {SQLITE_FILENAME})"
