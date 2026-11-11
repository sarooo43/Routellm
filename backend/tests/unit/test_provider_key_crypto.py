"""Unit tests for BYOK provider-key encryption helpers."""

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.core import provider_key_crypto
from app.core.provider_key_crypto import (
    ProviderKeyEncryptionError,
    decrypt_provider_key,
    encrypt_provider_key,
    provider_key_hint,
)


@pytest.fixture(autouse=True)
def _isolate_generated_key_cache(monkeypatch, tmp_path):
    """Never touch the real data dir: isolated data dir + no cached key."""
    monkeypatch.setenv("ROUTELLM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(provider_key_crypto, "_generated_key_cache", None)
    yield
    monkeypatch.delenv("ROUTELLM_DATA_DIR", raising=False)
    monkeypatch.setattr(provider_key_crypto, "_generated_key_cache", None)


@pytest.fixture
def encryption_key(monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setattr(settings, "PROVIDER_KEY_ENCRYPTION_KEY", key)
    return key


def test_encrypt_decrypt_roundtrip_and_ciphertext_not_raw(encryption_key):
    raw = "gsk_test_1234567890abcdef"
    token = encrypt_provider_key(raw)
    assert token != raw
    assert raw not in token
    assert decrypt_provider_key(token) == raw


def test_encrypt_strips_whitespace(encryption_key):
    token = encrypt_provider_key("  gsk_test_1234567890abcdef  ")
    assert decrypt_provider_key(token) == "gsk_test_1234567890abcdef"


def test_hint_only_exposes_last_four(encryption_key):
    assert provider_key_hint("gsk_test_1234567890abcdef") == "****cdef"
    assert provider_key_hint("abcd") == "****"


def test_missing_env_key_falls_back_to_generated_data_dir_key(
    monkeypatch, tmp_path
):
    """Local-first: an unset env key auto-uses (and creates) the data-dir key."""
    monkeypatch.setattr(settings, "PROVIDER_KEY_ENCRYPTION_KEY", None)
    monkeypatch.setenv("ROUTELLM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        provider_key_crypto, "_generated_key_cache", None
    )
    raw = "gsk_test_1234567890abcdef"
    token = encrypt_provider_key(raw)
    assert decrypt_provider_key(token) == raw
    # The generated key file exists and is invalid-free valid Fernet material.
    key_file = tmp_path / provider_key_crypto.ENCRYPTION_KEY_FILENAME
    assert key_file.is_file()
    Fernet(key_file.read_bytes().strip())
    # Deleting the key file makes previously stored keys unreadable.
    key_file.unlink()
    monkeypatch.setattr(provider_key_crypto, "_generated_key_cache", None)
    with pytest.raises(ProviderKeyEncryptionError):
        decrypt_provider_key(token)


def test_no_key_source_at_all_fails_closed(monkeypatch):
    """Fails closed when neither env nor data-dir key can be resolved."""
    monkeypatch.setattr(settings, "PROVIDER_KEY_ENCRYPTION_KEY", None)
    monkeypatch.setattr(
        provider_key_crypto, "_load_or_generate_data_dir_key", lambda: b""
    )
    with pytest.raises(ProviderKeyEncryptionError):
        encrypt_provider_key("gsk_test_1234567890abcdef")


def test_invalid_encryption_key_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "PROVIDER_KEY_ENCRYPTION_KEY", "not-a-fernet-key")
    with pytest.raises(ProviderKeyEncryptionError):
        encrypt_provider_key("gsk_test_1234567890abcdef")


def test_wrong_encryption_key_cannot_decrypt(monkeypatch, encryption_key):
    token = encrypt_provider_key("gsk_test_1234567890abcdef")
    monkeypatch.setattr(
        settings, "PROVIDER_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8")
    )
    with pytest.raises(ProviderKeyEncryptionError):
        decrypt_provider_key(token)


def test_empty_provider_key_rejected(encryption_key):
    with pytest.raises(ProviderKeyEncryptionError):
        encrypt_provider_key("   ")
