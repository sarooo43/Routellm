"""Encryption-at-rest helpers for per-caller provider API keys (BYOK).

Provider keys are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) before they
touch the database. The symmetric key comes only from the environment variable
``PROVIDER_KEY_ENCRYPTION_KEY``; it is never hardcoded or derived from caller
input. Raw provider keys are decrypted only in memory for the duration of a
routing request and are never logged or returned by the API.
"""

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class ProviderKeyEncryptionError(RuntimeError):
    """Raised when provider-key encryption is unavailable or a token is invalid."""


def _fernet() -> Fernet:
    """Build a Fernet instance from the configured encryption key."""
    key = settings.PROVIDER_KEY_ENCRYPTION_KEY
    if not key:
        raise ProviderKeyEncryptionError(
            "PROVIDER_KEY_ENCRYPTION_KEY is not configured. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise ProviderKeyEncryptionError(
            "PROVIDER_KEY_ENCRYPTION_KEY is not a valid Fernet key"
        ) from exc


def encrypt_provider_key(raw_key: str) -> str:
    """Return a Fernet token for a raw provider key."""
    if not raw_key or not raw_key.strip():
        raise ProviderKeyEncryptionError("Provider key must not be empty")
    return _fernet().encrypt(raw_key.strip().encode("utf-8")).decode("utf-8")


def decrypt_provider_key(token: str) -> str:
    """Decrypt a stored Fernet token back to the raw provider key."""
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ProviderKeyEncryptionError(
            "Stored provider key could not be decrypted with the configured key"
        ) from exc


def provider_key_hint(raw_key: str) -> str:
    """Return a non-secret display hint (last 4 characters only)."""
    cleaned = raw_key.strip()
    if len(cleaned) <= 4:
        return "****"
    return f"****{cleaned[-4:]}"
