"""Encryption-at-rest helpers for per-caller provider API keys (BYOK).

Provider keys are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) before they
touch the database. The symmetric key comes from, in order:

1. the ``PROVIDER_KEY_ENCRYPTION_KEY`` environment variable (cloud/self-host);
2. a key file auto-generated once in the local data directory
   (``<data dir>/provider_key_encryption.key``) — the local-first path. The
   file is created with restrictive permissions where the OS supports it and
   its contents are never logged.

Consequence for local users: deleting the key file makes every stored provider
key unreadable (Fernet has no recovery), so back the file up before wiping the
data directory. Raw provider keys are decrypted only in memory for the duration
of a routing request and are never logged or returned by the API.
"""

import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Key file name inside the data directory (see app.runtime.data_dir).
ENCRYPTION_KEY_FILENAME = "provider_key_encryption.key"

_generated_key_cache: Optional[bytes] = None


class ProviderKeyEncryptionError(RuntimeError):
    """Raised when provider-key encryption is unavailable or a token is invalid."""


def _load_or_generate_data_dir_key() -> bytes:
    """Read the auto-generated Fernet key from the data dir, creating it once.

    Never raises on first run: generation failure (fully read-only host)
    returns an empty result and BYOK keeps failing closed with a clear 503.
    The key value itself is never logged.
    """
    global _generated_key_cache
    if _generated_key_cache is not None:
        return _generated_key_cache

    from app.runtime import data_dir

    path = data_dir() / ENCRYPTION_KEY_FILENAME
    try:
        if path.is_file():
            key = path.read_bytes().strip()
            if key:
                Fernet(key)  # validate before caching
                _generated_key_cache = key
                return key
            logger.warning("provider_key_encryption_file_empty", path=path.name)
        key = Fernet.generate_key()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key + b"\n")
        try:
            os.chmod(path, 0o600)  # best effort; no-op on plain Windows
        except OSError:
            pass
        _generated_key_cache = key
        logger.info(
            "provider_key_encryption_key_generated",
            path=path.name,
            detail=(
                "A Fernet key was generated once and stored in the data dir. "
                "Deleting it makes stored provider keys unreadable."
            ),
        )
        return key
    except Exception as e:
        logger.warning(
            "provider_key_encryption_key_unavailable",
            error=str(e).strip()[:200],
        )
        return b""


def _fernet() -> Fernet:
    """Build a Fernet instance (env key first, then the local data-dir key)."""
    key = settings.PROVIDER_KEY_ENCRYPTION_KEY
    if not key:
        key_bytes = _load_or_generate_data_dir_key()
        if not key_bytes:
            raise ProviderKeyEncryptionError(
                "Provider key encryption is unavailable: no "
                "PROVIDER_KEY_ENCRYPTION_KEY and the data dir could not supply "
                "an auto-generated key file."
            )
        key = key_bytes.decode("utf-8")
    try:
        return Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise ProviderKeyEncryptionError(
            "PROVIDER_KEY_ENCRYPTION_KEY is not a valid Fernet key"
        ) from exc


def _reset_generated_key_cache() -> None:
    """Test/ops helper: forget the process-local generated key cache."""
    global _generated_key_cache
    _generated_key_cache = None


def encryption_available() -> bool:
    """True when provider-key encryption can run on this host (cached).

    Resolves the key source the same way :func:`_fernet` does (env first, then
    the auto-generated data-dir key) without doing any work beyond one load.
    """
    if settings.PROVIDER_KEY_ENCRYPTION_KEY:
        try:
            _fernet()
            return True
        except ProviderKeyEncryptionError:
            return False
    return bool(_load_or_generate_data_dir_key())


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
