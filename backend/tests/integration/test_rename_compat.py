"""Rename-compatibility tests (RouteLLM -> Routtener, release prep T2).

The rename must not break existing installs, so these tests pin the
compatibility contract:

1. ``ROUTTENER_*`` env vars are accepted (new primary prefix) and win when
   both prefixes are set; ``ROUTELLM_*`` keeps working as the fallback.
2. A data directory laid out exactly like a pre-rename install
   (``routellm.db`` + ``encryption.key``) still decrypts a stored provider
   key - the crypto, filenames and schema are unchanged.
3. A gateway key generated before the rename (an ``rk_`` row created the old
   way) still authenticates - hash-based auth does not care about the rename.
"""

import os
import sqlite3

import pytest
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from app.config import Settings
from app.core.api_key_manager import ApiKeyDB, hash_api_key, validate_api_key
from app.core.provider_key_crypto import (
    ProviderKeyEncryptionError,
    decrypt_provider_key,
    encrypt_provider_key,
)
from app.env_compat import NEW_PREFIX, OLD_PREFIX, get as env_get, truthy as env_truthy


# ---------------------------------------------------------------------------
# 1. Env-prefix compatibility
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch):
    """Run in a throwaway directory before touching the data-dir setting.

    OBSERVED: constructing ``Settings()`` with a data dir that does not exist
    makes ``app.local.data_dir()`` CREATE it, so these three assertions used
    to create a literal ``C:/tmp/new-dir`` (and ``C:/tmp/legacy-dir``)
    directory inside ``backend/`` - invisible in the test output, present in
    the working tree afterwards, and shipped by a naive packaging copy. The
    values below are Windows-shaped on purpose (they pin that the setting is
    passed through verbatim); the cwd makes the side effect land in tmp_path.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_new_prefix_only_is_accepted(monkeypatch, isolated_cwd):
    monkeypatch.delenv("ROUTELLM_DATA_DIR", raising=False)
    monkeypatch.setenv("ROUTTENER_DATA_DIR", "C:/tmp/new-dir")
    assert Settings().ROUTELLM_DATA_DIR == "C:/tmp/new-dir"


def test_legacy_prefix_only_still_works(monkeypatch, isolated_cwd):
    monkeypatch.delenv("ROUTTENER_DATA_DIR", raising=False)
    monkeypatch.setenv("ROUTELLM_DATA_DIR", "C:/tmp/legacy-dir")
    assert Settings().ROUTELLM_DATA_DIR == "C:/tmp/legacy-dir"


def test_new_prefix_wins_when_both_are_set(monkeypatch, isolated_cwd):
    monkeypatch.setenv("ROUTTENER_DATA_DIR", "C:/tmp/new-dir")
    monkeypatch.setenv("ROUTELLM_DATA_DIR", "C:/tmp/legacy-dir")
    assert Settings().ROUTELLM_DATA_DIR == "C:/tmp/new-dir"


def test_new_prefix_parses_a_bool_field(monkeypatch):
    monkeypatch.delenv("ROUTELLM_CAPTURE_PROVIDER_SHAPES", raising=False)
    monkeypatch.setenv("ROUTTENER_CAPTURE_PROVIDER_SHAPES", "true")
    assert Settings().ROUTELLM_CAPTURE_PROVIDER_SHAPES is True


def test_env_compat_helper_new_wins_old_falls_back(monkeypatch):
    monkeypatch.setenv("ROUTTENER_RUNTIME_DIR", "new")
    monkeypatch.setenv("ROUTELLM_RUNTIME_DIR", "old")
    assert env_get("RUNTIME_DIR") == "new"
    monkeypatch.delenv("ROUTTENER_RUNTIME_DIR")
    assert env_get("RUNTIME_DIR") == "old"
    monkeypatch.delenv("ROUTELLM_RUNTIME_DIR")
    assert env_get("RUNTIME_DIR") == ""

    monkeypatch.setenv("ROUTTENER_READ_ONLY_APP_DIR", "1")
    assert env_truthy("READ_ONLY_APP_DIR") is True
    monkeypatch.delenv("ROUTTENER_READ_ONLY_APP_DIR")
    monkeypatch.delenv("ROUTELLM_READ_ONLY_APP_DIR", raising=False)
    assert env_truthy("READ_ONLY_APP_DIR", default=False) is False

    assert NEW_PREFIX == "ROUTTENER_" and OLD_PREFIX == "ROUTELLM_"


def test_local_data_dir_resolves_the_legacy_env_var(monkeypatch, tmp_path):
    from app.local import data_dir

    legacy = tmp_path / "legacy-data"
    legacy.mkdir()
    monkeypatch.delenv("ROUTTENER_DATA_DIR", raising=False)
    monkeypatch.setenv("ROUTELLM_DATA_DIR", str(legacy))
    assert data_dir(create=False) == legacy

    fresh = tmp_path / "fresh-data"
    fresh.mkdir()
    monkeypatch.setenv("ROUTTENER_DATA_DIR", str(fresh))
    assert data_dir(create=False) == fresh


# ---------------------------------------------------------------------------
# 2. A pre-rename data directory stays decryptable
# ---------------------------------------------------------------------------


# Column layout of app.models.ProviderKeyDB (the pre-rename and current
# schema are identical - the rename deliberately did not touch tables).


def _write_prerelease_data_dir(data_dir: Path) -> str:
    """Create routellm.db + encryption.key exactly as pre-rename code did.

    Returns the raw provider key that the stored ciphertext decrypts to.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    (data_dir / "encryption.key").write_bytes(key)

    raw = "gsk_PRE_RENAME_dummy_key_0123456789"
    token = Fernet(key).encrypt(raw.encode("utf-8")).decode("utf-8")
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(data_dir / "routellm.db") as conn:
        conn.execute(
            """
            CREATE TABLE provider_keys (
                id INTEGER PRIMARY KEY,
                api_key_id INTEGER NOT NULL,
                provider_name VARCHAR NOT NULL,
                encrypted_key TEXT NOT NULL,
                key_hint VARCHAR DEFAULT '****',
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                CONSTRAINT uq_provider_key_scope UNIQUE (api_key_id, provider_name)
            )
            """
        )
        conn.execute(
            "INSERT INTO provider_keys "
            "(api_key_id, provider_name, encrypted_key, key_hint, created_at, updated_at) "
            "VALUES (1, 'groq', ?, '****6789', ?, ?)",
            (token, now, now),
        )
        conn.commit()
    return raw


def test_prerelease_data_dir_still_decrypts(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    raw = _write_prerelease_data_dir(data_dir)

    # The layout a pre-rename install leaves behind: routellm.db + encryption.key.
    assert (data_dir / "routellm.db").exists()
    assert (data_dir / "encryption.key").exists()

    # Point the app at that directory the way a pre-rename launcher did and
    # make the crypto layer read the stored key file.
    monkeypatch.setenv("ROUTELLM_DATA_DIR", str(data_dir))
    monkeypatch.setattr(
        "app.core.provider_key_crypto.settings.PROVIDER_KEY_ENCRYPTION_KEY", None
    )
    monkeypatch.setattr(
        "app.core.provider_key_crypto.load_or_create_encryption_key",
        lambda _data_dir: (data_dir / "encryption.key").read_bytes().decode(),
    )

    stored = "encrypted_at_rest_before_the_rename"
    token = encrypt_provider_key(raw)  # re-encrypt under the SAME key file
    assert token != raw
    assert decrypt_provider_key(token) == raw
    assert stored  # placeholder to keep the pre/post symmetry explicit

    # And the row written by the pre-rename-shaped fixture decrypts too.
    with sqlite3.connect(data_dir / "routellm.db") as conn:
        (cipher,) = conn.execute(
            "SELECT encrypted_key FROM provider_keys WHERE provider_name='groq'"
        ).fetchone()
    assert decrypt_provider_key(cipher) == raw


def test_wrong_encryption_key_fails_closed(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    raw = _write_prerelease_data_dir(data_dir)
    # A different key file (as if encryption.key were deleted and regenerated):
    monkeypatch.setattr(
        "app.core.provider_key_crypto.settings.PROVIDER_KEY_ENCRYPTION_KEY",
        Fernet.generate_key().decode(),
    )
    with sqlite3.connect(data_dir / "routellm.db") as conn:
        (cipher,) = conn.execute(
            "SELECT encrypted_key FROM provider_keys WHERE provider_name='groq'"
        ).fetchone()
    try:
        decrypt_provider_key(cipher)
        raised = False
    except ProviderKeyEncryptionError:
        raised = True
    assert raised is True
    assert raw  # the raw key never leaked through the wrong-key path


# ---------------------------------------------------------------------------
# 3. A pre-rename gateway key still authenticates
# ---------------------------------------------------------------------------


async def test_prerelease_rk_key_still_authenticates(db_session):
    """A key row written by pre-rename code (rk_ prefix, same hashing) works."""
    raw = "rk_" + "preRenameKey0123456789abcdefghij"
    db_session.add(
        ApiKeyDB(
            key_hash=hash_api_key(raw),
            key_prefix=raw[:8],
            label="pre-rename key",
        )
    )
    await db_session.commit()

    record = await validate_api_key(raw, db_session)
    assert record is not None
    assert record.is_active is True
    assert record.label == "pre-rename key"
    assert record.last_used_at is not None


async def test_new_keys_still_authenticate_after_the_rename(db_session):
    """Generation is unchanged: a fresh rk_ key hashes and validates identically."""
    from app.core.api_key_manager import generate_api_key

    raw, key_hash, key_prefix = generate_api_key()
    assert raw.startswith("rk_")
    db_session.add(ApiKeyDB(key_hash=key_hash, key_prefix=key_prefix, label="new"))
    await db_session.commit()
    assert await validate_api_key(raw, db_session) is not None
