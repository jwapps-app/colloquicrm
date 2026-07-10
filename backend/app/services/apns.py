"""APNs sender for the iOS companion app.

Token-based auth: an ES256 JWT signed with the .p8 key, sent over HTTP/2 to
Apple's push hosts. Each device row carries its own environment (sandbox for
Xcode builds, production for TestFlight/App Store) and is routed to the
matching host. Stale tokens (410 Unregistered / BadDeviceToken) are pruned.

Like the Colloqui chat sender, every push is best-effort: APNs being down or
misconfigured must never break or delay a CRM action.
"""

import logging
import time
import uuid

import httpx
import jwt
from sqlalchemy import delete, select

from app.config import settings
from app.models import DeviceToken

log = logging.getLogger("apns")

# Apple accepts provider tokens 20-60 minutes old; refresh at 45.
JWT_REFRESH_SECONDS = 45 * 60

_jwt_cache: tuple[str, float] | None = None
_client: httpx.AsyncClient | None = None


def is_configured() -> bool:
    return bool(settings.apns_key_id and settings.apns_team_id and settings.apns_key)


def _auth_token() -> str:
    global _jwt_cache
    now = time.time()
    if _jwt_cache is not None and now - _jwt_cache[1] < JWT_REFRESH_SECONDS:
        return _jwt_cache[0]
    key = settings.apns_key.replace("\\n", "\n")
    token = jwt.encode(
        {"iss": settings.apns_team_id, "iat": int(now)},
        key,
        algorithm="ES256",
        headers={"kid": settings.apns_key_id},
    )
    _jwt_cache = (token, now)
    return token


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(http2=True, timeout=10.0)
    return _client


async def send_to_user(
    db, user_id: uuid.UUID, title: str, body: str, extra: dict | None = None
) -> int:
    """Push to every registered device of a user. Returns devices reached —
    0 means the user has no (working) devices and the caller may fall back
    to another channel."""
    if not is_configured():
        return 0
    tokens = (
        (await db.execute(select(DeviceToken).where(DeviceToken.user_id == user_id)))
        .scalars()
        .all()
    )
    sent = 0
    for t in tokens:
        if await _send_one(db, t, title, body, extra or {}):
            sent += 1
    return sent


async def _send_one(db, device: DeviceToken, title: str, body: str, extra: dict) -> bool:
    host = (
        settings.apns_sandbox_host
        if device.environment == "sandbox"
        else settings.apns_production_host
    )
    payload = {
        "aps": {"alert": {"title": title, "body": body}, "sound": "default"},
        **extra,
    }
    headers = {
        "authorization": f"bearer {_auth_token()}",
        "apns-topic": settings.apns_topic,
        "apns-push-type": "alert",
        "apns-priority": "10",
    }
    try:
        resp = await _get_client().post(
            f"{host}/3/device/{device.token}", json=payload, headers=headers
        )
    except httpx.HTTPError as exc:
        log.warning("APNs send to %s… failed: %s", device.token[:8], exc)
        return False
    if resp.status_code == 200:
        return True
    try:
        reason = resp.json().get("reason", "")
    except Exception:
        reason = ""
    if resp.status_code == 410 or reason in ("BadDeviceToken", "Unregistered"):
        # The device uninstalled the app or the token rotated — drop the row
        # so we stop paying for dead sends.
        await db.execute(delete(DeviceToken).where(DeviceToken.id == device.id))
        log.info("Pruned dead device token %s… (%s)", device.token[:8], reason or resp.status_code)
    else:
        log.warning(
            "APNs rejected push to %s…: %s %s", device.token[:8], resp.status_code, reason
        )
    return False
