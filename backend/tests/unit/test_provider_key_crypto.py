"""Unit tests for BYOK provider-key encryption helpers."""

import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.core.provider_key_crypto import (
    ProviderKeyEncryptionError,
    decrypt_provider_key,
    encrypt_provider_key,
    provider_key_hint,
)


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


def test_missing_encryption_key_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "PROVIDER_KEY_ENCRYPTION_KEY", None)
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
