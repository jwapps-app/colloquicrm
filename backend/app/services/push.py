"""Push to the iOS companion app via the shared push relay.

The CRM never holds Apple credentials. Like every other app in the fleet it
POSTs each notification to the push relay (X-API-Key auth, key scoped to this
bundle id); the relay signs with the team's .p8 and forwards to APNs. Dead
tokens are reported back in the relay's 502 detail and pruned here.

Every push is best-effort: the relay being down or unconfigured must never
break or delay a CRM action. Unset env = feature dark, chat DMs keep working.
"""

import logging
import uuid

import httpx
from sqlalchemy import delete, select

from app.config import settings
from app.models import DeviceToken

log = logging.getLogger("push")

# Apple reasons (surfaced in the relay's 502 detail) that mean the token is
# permanently gone — uninstalled, rotated, or registered for another app.
DEAD_TOKEN_REASONS = ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic")

_client: httpx.AsyncClient | None = None


def is_configured() -> bool:
    return bool(
        settings.push_relay_url and settings.push_relay_api_key and settings.apns_topic
    )


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10.0)
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
    payload = {
        "bundle_id": settings.apns_topic,
        "device_token": device.token,
        "title": title,
        "body": body,
        "custom_data": extra,
        "sandbox": device.environment == "sandbox",
    }
    try:
        resp = await _get_client().post(
            settings.push_relay_url.rstrip("/") + "/notify",
            json=payload,
            headers={"X-API-Key": settings.push_relay_api_key},
        )
    except httpx.HTTPError as exc:
        log.warning("Relay push to %s… failed: %s", device.token[:8], exc)
        return False
    if resp.status_code == 200:
        return True
    try:
        detail = str(resp.json().get("detail", ""))
    except Exception:
        detail = ""
    if any(reason in detail for reason in DEAD_TOKEN_REASONS):
        # The device uninstalled the app or the token rotated — drop the row
        # so we stop paying for dead sends. Commit here: notification callers
        # are read-only sessions that never commit themselves.
        await db.execute(delete(DeviceToken).where(DeviceToken.id == device.id))
        await db.commit()
        log.info("Pruned dead device token %s… (%s)", device.token[:8], detail)
    else:
        log.warning(
            "Relay rejected push to %s…: %s %s", device.token[:8], resp.status_code, detail
        )
    return False
