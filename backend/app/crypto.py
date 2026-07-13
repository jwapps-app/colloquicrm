"""Encryption-at-rest for sensitive secret columns.

Third-party secrets (Google refresh/access tokens, RingCentral client secret
and JWT, the Colloqui api_key) and TOTP seeds are stored ciphertext in the
database so a DB dump or backup leak does not hand an attacker live
credentials. Encryption is symmetric (Fernet) with a key held OUTSIDE the
database.

Key source (in priority order):
  1. settings.secret_encryption_key (env SECRET_ENCRYPTION_KEY) — a urlsafe
     base64 32-byte Fernet key. Recommended for production; generate with
       python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  2. Fallback: derived deterministically from settings.secret_key so the
     feature works out-of-the-box on existing deploys with no new env var.

OPERATIONAL CAVEAT: with the fallback, the encryption key is a function of
SECRET_KEY. Rotating SECRET_KEY would then orphan every encrypted secret
(they become unreadable). To rotate SECRET_KEY safely, first pin the current
derived key into SECRET_ENCRYPTION_KEY so encryption is decoupled from it.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

from app.config import settings

# Fernet tokens are urlsafe-base64 of a versioned struct; v1 tokens begin with
# the 0x80 version byte, which base64-encodes to the "gAAAAA" prefix. Used as a
# cheap pre-check before attempting a real decrypt.
_FERNET_PREFIX = "gAAAAA"


def _derive_key_from_secret(secret_key: str) -> bytes:
    """Deterministic 32-byte urlsafe-base64 Fernet key from SECRET_KEY."""
    digest = hashlib.sha256(secret_key.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def _build_fernet() -> Fernet:
    configured = (settings.secret_encryption_key or "").strip()
    if configured:
        key = configured.encode()
    else:
        key = _derive_key_from_secret(settings.secret_key)
    return Fernet(key)


_fernet = _build_fernet()


def encrypt(value: str | None) -> str | None:
    """Encrypt a plaintext string to a Fernet token. None passes through."""
    if value is None:
        return None
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    """Decrypt a Fernet token back to plaintext.

    Backward-compatible: a value that is not a valid Fernet token is assumed to
    be a legacy pre-encryption plaintext row and is returned AS-IS. This lets
    the app read old rows before (and after) they are migrated.
    """
    if value is None:
        return None
    if not value.startswith(_FERNET_PREFIX):
        return value
    try:
        return _fernet.decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        # Not actually ciphertext (a plaintext value that happens to share the
        # prefix, or corrupt data) — return unchanged rather than crash.
        return value


def is_encrypted(value: str | None) -> bool:
    """True if the value is a decryptable Fernet token."""
    if value is None or not value.startswith(_FERNET_PREFIX):
        return False
    try:
        _fernet.decrypt(value.encode())
        return True
    except (InvalidToken, ValueError):
        return False


class EncryptedString(TypeDecorator):
    """A String/Text column whose value is transparently encrypted at rest.

    Values are Fernet-encrypted on write (process_bind_param) and decrypted on
    read (process_result_value), with legacy plaintext rows passed through
    unchanged on read. Route and service code never sees ciphertext — it reads
    and writes plaintext through the ORM as before.

    A Fernet token of a short secret is ~180-240 chars, so the underlying
    column must be wide enough (see the encrypt-secrets migration). Pass a
    length for a VARCHAR; omit it for TEXT (used for the larger token columns).
    """

    impl = String
    cache_ok = True

    def __init__(self, length: int | None = None, **kwargs):
        super().__init__(length=length, **kwargs)

    def process_bind_param(self, value, dialect):
        return encrypt(value)

    def process_result_value(self, value, dialect):
        return decrypt(value)
