"""Local-first runtime contracts: data dir, SQLite default, generated key file.

These pin the promises the "clone and run" flow depends on:

* the data directory is overridable and survives a changed working directory,
* ``DATABASE_URL`` always wins over the SQLite default,
* the BYOK encryption key is generated exactly once, reused afterwards, and
  never appears in a log line,
* restrictive permissions are requested on POSIX and are not fatal elsewhere.
"""

import os
import stat
from pathlib import Path

import pytest

from app import local
from app.config import settings


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the runtime at a throwaway directory for one test."""
    target = tmp_path / "data"
    monkeypatch.setenv(local.DATA_DIR_ENV, str(target))
    monkeypatch.setattr(settings, "ROUTELLM_DATA_DIR", "", raising=False)
    return target


# --------------------------------------------------------------------------
# Data directory resolution
# --------------------------------------------------------------------------


def test_env_var_override_is_created_on_demand(data_dir):
    assert not data_dir.exists()
    resolved = local.data_dir()
    assert resolved == data_dir
    assert resolved.is_dir()


def test_settings_value_is_honoured_when_the_env_var_is_unset(tmp_path, monkeypatch):
    """A .env entry must work, not only an exported variable."""
    monkeypatch.delenv(local.DATA_DIR_ENV, raising=False)
    configured = tmp_path / "from-settings"
    monkeypatch.setattr(settings, "ROUTELLM_DATA_DIR", str(configured))
    assert local.data_dir() == configured


def test_relative_override_is_resolved_against_the_cwd(tmp_path, monkeypatch):
    """The sqlite path must not depend on where the process was started."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(local.DATA_DIR_ENV, "data")
    monkeypatch.setattr(settings, "ROUTELLM_DATA_DIR", "", raising=False)
    assert local.data_dir() == (tmp_path / "data").resolve()


def test_default_is_the_repo_data_directory(monkeypatch):
    """No configuration at all still yields a single, predictable place."""
    monkeypatch.delenv(local.DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(settings, "ROUTELLM_DATA_DIR", "", raising=False)
    resolved = local.data_dir(create=False)
    assert resolved.name == "data"
    assert resolved.parent == Path(local.__file__).resolve().parent.parent.parent


def test_read_only_checkout_falls_back_to_a_user_directory(tmp_path, monkeypatch):
    """An un-writable checkout (release zip in /opt, Program Files) still runs."""
    monkeypatch.delenv(local.DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(settings, "ROUTELLM_DATA_DIR", "", raising=False)
    fallback = tmp_path / "user-data"
    monkeypatch.setattr(local, "_is_writable", lambda _p: False)
    monkeypatch.setattr(local, "_user_data_dir", lambda *_a, **_k: fallback)
    assert local.data_dir() == fallback
    assert fallback.is_dir()


def test_describe_data_location_mentions_the_sqlite_file(data_dir):
    described = local.describe_data_location()
    assert str(data_dir) in described
    assert local.SQLITE_FILENAME in described


# --------------------------------------------------------------------------
# Database URL
# --------------------------------------------------------------------------


def test_default_database_url_points_at_the_sqlite_file(data_dir):
    url = local.default_database_url()
    assert url.startswith("sqlite+aiosqlite:///")
    assert url.endswith(local.SQLITE_FILENAME)
    assert str(data_dir) in url


def test_explicit_database_url_always_wins():
    postgres = "postgresql://user:secret@db.example.com/routellm"
    assert local.resolve_database_url(postgres) == postgres


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_database_url_falls_back_to_sqlite(data_dir, blank):
    assert local.resolve_database_url(blank).startswith("sqlite+aiosqlite:///")
    assert str(data_dir) in local.resolve_database_url(blank)


# --------------------------------------------------------------------------
# Encryption key file
# --------------------------------------------------------------------------


def test_key_is_created_once_and_reused(data_dir):
    first = local.load_or_create_encryption_key(None)
    assert first
    key_file = data_dir / local.ENCRYPTION_KEY_FILENAME
    assert key_file.exists()

    second = local.load_or_create_encryption_key(None)
    assert second == first
    # One file, one key: a second run must not rotate (it would orphan every
    # stored provider key).
    assert key_file.read_text(encoding="utf-8").strip() == first


def test_configured_key_wins_and_is_never_written_to_disk(data_dir):
    configured = "3wZ0Y2b2Y2N0Z2Q0c2VjcmV0LWtleS1maWxlLXRlc3Q="
    assert local.load_or_create_encryption_key(configured) == configured
    assert not (data_dir / local.ENCRYPTION_KEY_FILENAME).exists()


def test_generated_key_is_a_usable_fernet_key(data_dir):
    from cryptography.fernet import Fernet

    key = local.load_or_create_encryption_key(None)
    token = Fernet(key.encode("utf-8")).encrypt(b"provider-secret")
    assert Fernet(key.encode("utf-8")).decrypt(token) == b"provider-secret"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_key_file_is_owner_only_on_posix(data_dir):
    local.load_or_create_encryption_key(None)
    mode = stat.S_IMODE((data_dir / local.ENCRYPTION_KEY_FILENAME).stat().st_mode)
    assert mode == 0o600


def test_generated_key_is_never_logged(data_dir, capsys):
    """Log lines may name the path, never the secret.

    structlog is configured with a stdout sink (containers treat stdout as the
    log file), so this asserts on captured output rather than caplog.
    """
    key = local.load_or_create_encryption_key(None)
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert key
    assert key not in output
    assert "encryption_key" in output  # the event name is fine...
    assert str(data_dir) in output  # ...and so is the path


def test_unwritable_location_returns_the_key_without_persisting(tmp_path, monkeypatch):
    """A read-only data dir must not take the whole app down."""

    def _explode(*_args, **_kwargs):
        raise OSError("read-only filesystem (simulated)")

    monkeypatch.setattr(local, "encryption_key_path", lambda: tmp_path / "ro" / "encryption.key")
    monkeypatch.setattr(local, "data_dir", lambda **_kwargs: tmp_path / "ro")
    monkeypatch.setattr(local.os, "open", _explode, raising=False)
    key = local.load_or_create_encryption_key(None)
    # Usable for this process; the caller is told it will not survive a restart.
    assert key
    assert not (tmp_path / "ro" / "encryption.key").exists()
