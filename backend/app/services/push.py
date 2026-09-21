"""Push to the iOS companion app via the shared push relay.

The CRM never holds Apple credentials. Like every other app in the fleet it
POSTs each notification to the push relay (X-API-Key auth, key scoped to this
bundle id); the relay signs with the team's .p8 and forwards to APNs. A dead
token comes back as a 410 naming Apple's reason, and is pruned here.

Every push is best-effort: the relay being down or unconfigured must never
break or delay a CRM action. Unset env = feature dark, chat DMs keep working.
"""

import logging
import uuid

import httpx
from sqlalchemy import delete, select

from app.config import settings
from app.models import DeviceToken, User

log = logging.getLogger("push")

# Apple reasons that mean the token is permanently gone — uninstalled,
# rotated, or registered for another app. The relay reports them as a 410
# whose `reason` is exactly one of these; nothing else is ever grounds to
# delete a device (a 422's detail echoes our own title/body back, so matching
# on text would let a task named "Unregistered" delete a live token).
DEAD_TOKEN_REASONS = ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic")

_client: httpx.AsyncClient | None = None


def is_configured() -> bool:
    return bool(
        settings.push_relay_url and settings.push_relay_api_key and settings.apns_topic
    )


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=10.0, limits=httpx.Limits(max_connections=10, max_keepalive_connections=5)
        )
    return _client


async def aclose() -> None:
    """Drain the shared pool; called once from app shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def send_to_user(
    db,
    user_id: uuid.UUID,
    title: str,
    body: str,
    extra: dict | None = None,
    badge: int | None = None,
) -> int:
    """Push to every registered device of a user. Returns devices reached —
    0 means the user has no (working) devices and the caller may fall back
    to another channel."""
    if not is_configured():
        # The caller only gets here for a user who CHOSE app push — falling
        # back silently made a missing env var look like a routing bug.
        log.warning(
            "User %s wants app push but PUSH_RELAY_URL/PUSH_RELAY_API_KEY "
            "are not configured; falling back to chat",
            user_id,
        )
        return 0
    # Joined to the user so a deactivated account is never pushed to, even if
    # a device row outlives the deactivation cleanup (or re-registers through
    # some path that forgot to check).
    tokens = (
        (
            await db.execute(
                select(DeviceToken)
                .join(User, DeviceToken.user_id == User.id)
                .where(DeviceToken.user_id == user_id, User.is_active.is_(True))
            )
        )
        .scalars()
        .all()
    )
    if not tokens:
        log.warning("User %s wants app push but has no registered devices", user_id)
    sent = 0
    for t in tokens:
        if await _send_one(db, t, title, body, extra or {}, badge):
            sent += 1
    return sent


async def _post(payload: dict) -> httpx.Response | None:
    try:
        return await _get_client().post(
            settings.push_relay_url.rstrip("/") + "/notify",
            json=payload,
            headers={"X-API-Key": settings.push_relay_api_key},
        )
    except httpx.HTTPError as exc:
        log.warning("Relay push to %s… failed: %s", payload["device_token"][:8], exc)
        return None


def _dead_token_reason(resp: httpx.Response) -> str | None:
    if resp.status_code != 410:
        return None
    try:
        reason = resp.json().get("reason")
    except Exception:
        return None
    return reason if reason in DEAD_TOKEN_REASONS else None


async def _send_one(
    db, device: DeviceToken, title: str, body: str, extra: dict, badge: int | None = None
) -> bool:
    payload = {
        "bundle_id": settings.apns_topic,
        "device_token": device.token,
        "title": title,
        "body": body,
        "custom_data": extra,
        "sandbox": device.environment == "sandbox",
    }
    if badge is not None:
        payload["badge"] = badge
    resp = await _post(payload)
    if resp is None:
        return False
    reason = _dead_token_reason(resp)
    if reason == "BadDeviceToken":
        # Apple says this for a perfectly good token sent to the wrong
        # environment (a TestFlight/App Store token recorded as sandbox, or the
        # reverse). Nothing was delivered, so trying the other side can't
        # double-notify — and it saves a live device from being deleted.
        other = "production" if payload["sandbox"] else "sandbox"
        retry = await _post({**payload, "sandbox": other == "sandbox"})
        if retry is not None and retry.status_code == 200:
            device.environment = other
            await db.commit()
            log.info(
                "Pushed '%s' to %s… after correcting its environment to %s",
                title, device.token[:8], other,
            )
            return True
        if retry is None or _dead_token_reason(retry) is None:
            # The second opinion never arrived (or wasn't a verdict on the
            # token) — not enough to delete on. Leave it for the next push.
            log.warning("Relay rejected push to %s…: BadDeviceToken", device.token[:8])
            return False
    if resp.status_code == 200:
        log.info("Pushed '%s' to %s… (%s)", title, device.token[:8], device.environment)
        return True
    if reason is not None:
        # The device uninstalled the app or the token rotated — drop the row
        # so we stop paying for dead sends. Commit here: notification callers
        # are read-only sessions that never commit themselves.
        await db.execute(delete(DeviceToken).where(DeviceToken.id == device.id))
        await db.commit()
        log.info("Pruned dead device token %s… (%s)", device.token[:8], reason)
        return False
    if resp.status_code == 429:
        log.warning(
            "Relay is rate-limiting pushes (retry after %s); dropped push to %s…",
            resp.headers.get("Retry-After", "?"), device.token[:8],
        )
        return False
    try:
        detail = str(resp.json().get("detail", ""))
    except Exception:
        detail = ""
    log.warning("Relay rejected push to %s…: %s %s", device.token[:8], resp.status_code, detail)
    return False
