import hashlib
import secrets

import anyio
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()


# argon2 is deliberately slow (tens of milliseconds of CPU per call) — run it
# on a worker thread so a login burst doesn't stall every other request on the
# event loop. The sync forms stay for module-level setup (the login decoy).


def hash_password_sync(password: str) -> str:
    return _hasher.hash(password)


def verify_password_sync(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, Argon2Error):
        return False


async def hash_password(password: str) -> str:
    return await anyio.to_thread.run_sync(hash_password_sync, password)


async def verify_password(password: str, password_hash: str) -> bool:
    return await anyio.to_thread.run_sync(verify_password_sync, password, password_hash)


def new_session_token() -> tuple[str, str]:
    """Returns (plaintext token for the client, sha256 hash for storage)."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
