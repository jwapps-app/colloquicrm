"""The OAuth round-trip: scopes, the consent URL, signed state, and the code exchange."""

import hashlib
import hmac
import logging
import time
import uuid
from urllib.parse import urlencode

from app.config import settings
from app.models import (
    GoogleAccount,
    GoogleIntegration,
)
from app.services.google.client import _post_form

log = logging.getLogger("google")


GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    GMAIL_SCOPE,
]


STATE_TTL_SECONDS = 600


def redirect_uri() -> str:
    return f"{settings.app_url.rstrip('/')}/api/v1/integrations/google/callback"


# ---- signed state (CSRF protection for the OAuth round-trip) ----
#
# The HMAC proves WE minted the state for that user; on its own it doesn't
# prove the browser finishing the flow is the one that started it — a valid
# state URL could be handed to a victim to bind the attacker's Google account
# to their CRM user. So the state also carries the hash of a nonce that lives
# only in a cookie on the starting browser; the callback requires both.

def hash_nonce(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


def make_state(user_id: uuid.UUID, nonce_hash: str) -> str:
    ts = str(int(time.time()))
    payload = f"{user_id}:{nonce_hash}:{ts}"
    sig = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def check_state(state: str) -> tuple[uuid.UUID, str] | None:
    """(user_id, nonce_hash) for a valid, unexpired state; None otherwise."""
    try:
        user_part, nonce_hash, ts, sig = state.rsplit(":", 3)
    except ValueError:
        return None
    payload = f"{user_part}:{nonce_hash}:{ts}"
    expected = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    if time.time() - int(ts) > STATE_TTL_SECONDS:
        return None
    try:
        return uuid.UUID(user_part), nonce_hash
    except ValueError:
        return None


def auth_url(client_id: str, user_id: uuid.UUID, nonce: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",  # guarantees a refresh_token on reconnect
        "state": make_state(user_id, hash_nonce(nonce)),
    }
    return f"{settings.google_auth_url}?{urlencode(params)}"


async def exchange_code(cfg: GoogleIntegration, code: str) -> dict:
    return await _post_form(
        settings.google_token_url,
        {
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri(),
        },
    )


def has_gmail_scope(account: GoogleAccount) -> bool:
    return GMAIL_SCOPE in (account.scopes or "")
