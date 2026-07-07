"""RingCentral integration: sync calls and texts onto People and Leads.

Auth is the JWT credentials flow — the org registers a private app in
RingCentral's developer console and pastes client id/secret + a user JWT.
The sync reads the account-wide Call Log and the extension Message Store,
keeps only events whose other party's number matches a known Person or Lead
(normalized to E.164), and drops the rest. Same philosophy as email sync:
this is a relationship record, not a phone archive.
"""

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Lead, Person, PhoneEvent, RingCentralIntegration, utcnow

log = logging.getLogger("ringcentral")

SYNC_INTERVAL_SECONDS = 600
BACKFILL_DAYS_DEFAULT = 0  # 0 = as far back as RingCentral retains


class RingCentralError(Exception):
    pass


def normalize_phone(raw: str | None, default_country: str = "1") -> str | None:
    """Loose E.164 normalization. US-centric default: 10 digits get +1."""
    if not raw:
        return None
    digits = re.sub(r"[^\d+]", "", str(raw))
    if not digits:
        return None
    if digits.startswith("+"):
        return "+" + re.sub(r"\D", "", digits)
    digits = re.sub(r"\D", "", digits)
    if len(digits) == 10:
        return f"+{default_country}{digits}"
    if len(digits) == 11 and digits.startswith(default_country):
        return f"+{digits}"
    if len(digits) > 11:
        return f"+{digits}"
    return None  # too short to be a real number (extensions etc.)


async def _post_form(url: str, data: dict, auth: tuple[str, str]) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, data=data, auth=auth)
    except httpx.HTTPError as exc:
        raise RingCentralError(f"Cannot reach RingCentral: {exc}") from exc
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error_description") or resp.json().get("message", "")
        except Exception:
            detail = resp.text[:150]
        raise RingCentralError(f"RingCentral auth failed ({resp.status_code}): {detail}")
    return resp.json()


async def _get_json(url: str, access: str, params: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.get(
                url, params=params, headers={"Authorization": f"Bearer {access}"}
            )
    except httpx.HTTPError as exc:
        raise RingCentralError(f"Cannot reach RingCentral: {exc}") from exc
    if resp.status_code >= 400:
        try:
            detail = (resp.json().get("errors") or [{}])[0].get("message", "")
        except Exception:
            detail = resp.text[:150]
        raise RingCentralError(
            f"RingCentral GET {url.split('?')[0].split('/restapi/')[-1]} failed "
            f"({resp.status_code}): {detail}"
        )
    return resp.json()


async def ensure_access_token(db, cfg: RingCentralIntegration) -> str:
    if cfg.access_token and cfg.access_expires_at:
        expires = cfg.access_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires > utcnow() + timedelta(minutes=2):
            return cfg.access_token
    tokens = await _post_form(
        f"{settings.ringcentral_base}/restapi/oauth/token",
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": cfg.jwt,
        },
        auth=(cfg.client_id, cfg.client_secret),
    )
    cfg.access_token = tokens["access_token"]
    cfg.access_expires_at = utcnow() + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    await db.flush()
    return cfg.access_token


async def fetch_own_numbers(access: str) -> list[str]:
    """The account's own phone numbers, so we can tell which side of a call
    is the contact."""
    numbers: list[str] = []
    page = 1
    while True:
        data = await _get_json(
            f"{settings.ringcentral_base}/restapi/v1.0/account/~/phone-number",
            access,
            {"perPage": 100, "page": page},
        )
        for rec in data.get("records", []):
            n = normalize_phone(rec.get("phoneNumber"))
            if n and n not in numbers:
                numbers.append(n)
        paging = data.get("paging") or {}
        if page >= int(paging.get("totalPages") or 1):
            return numbers
        page += 1


async def _crm_phone_map(db, org_id: uuid.UUID) -> dict[str, tuple[str, uuid.UUID]]:
    out: dict[str, tuple[str, uuid.UUID]] = {}
    rows = await db.execute(
        select(Person.id, Person.work_phone, Person.mobile_phone).where(Person.org_id == org_id)
    )
    for pid, work, mobile in rows:
        for p in (work, mobile):
            n = normalize_phone(p)
            if n:
                out[n] = ("person", pid)
    rows = await db.execute(
        select(Lead.id, Lead.work_phone, Lead.mobile_phone).where(Lead.org_id == org_id)
    )
    for lid, work, mobile in rows:
        for p in (work, mobile):
            n = normalize_phone(p)
            if n:
                out.setdefault(n, ("lead", lid))
    return out


def _parse_when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _known_rc_ids(db, org_id: uuid.UUID, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    rows = await db.execute(
        select(PhoneEvent.rc_id).where(PhoneEvent.org_id == org_id, PhoneEvent.rc_id.in_(ids))
    )
    return {r for (r,) in rows}


async def sync_calls(db, cfg: RingCentralIntegration, access: str, phone_map: dict) -> int:
    date_from = (utcnow() - timedelta(days=settings.ringcentral_backfill_days)).isoformat()
    stored = 0
    page = 1
    while True:
        params = {"view": "Simple", "perPage": 250, "page": page, "dateFrom": date_from}
        data = await _get_json(
            f"{settings.ringcentral_base}/restapi/v1.0/account/~/call-log", access, params
        )
        records = data.get("records", [])
        known = await _known_rc_ids(
            db, cfg.org_id, [f"call:{r.get('id')}" for r in records if r.get("id")]
        )
        for rec in records:
            rc_id = rec.get("id")
            if not rc_id or f"call:{rc_id}" in known:
                continue
            direction = (rec.get("direction") or "").lower()  # Inbound/Outbound
            other = rec.get("from") if direction == "inbound" else rec.get("to")
            other = other or {}
            number = normalize_phone(other.get("phoneNumber"))
            if not number or number not in phone_map:
                continue
            db.add(
                PhoneEvent(
                    org_id=cfg.org_id,
                    rc_id=f"call:{rc_id}",
                    kind="call",
                    direction=direction or "inbound",
                    other_number=number,
                    other_name=(other.get("name") or "")[:255] or None,
                    happened_at=_parse_when(rec.get("startTime")),
                    duration_seconds=rec.get("duration"),
                    result=(rec.get("result") or "")[:60] or None,
                    recording_id=str((rec.get("recording") or {}).get("id") or "") or None,
                )
            )
            stored += 1
        paging = data.get("paging") or {}
        if page >= int(paging.get("totalPages") or 1):
            break
        page += 1
    return stored


async def sync_sms(db, cfg: RingCentralIntegration, access: str, phone_map: dict) -> int:
    date_from = (utcnow() - timedelta(days=settings.ringcentral_backfill_days)).isoformat()
    stored = 0
    page = 1
    while True:
        params = {"messageType": "SMS", "perPage": 250, "page": page, "dateFrom": date_from}
        data = await _get_json(
            f"{settings.ringcentral_base}/restapi/v1.0/account/~/extension/~/message-store",
            access,
            params,
        )
        records = data.get("records", [])
        known = await _known_rc_ids(
            db, cfg.org_id, [f"sms:{r.get('id')}" for r in records if r.get("id")]
        )
        for rec in records:
            rc_id = rec.get("id")
            if not rc_id or f"sms:{rc_id}" in known:
                continue
            direction = (rec.get("direction") or "").lower()
            if direction == "inbound":
                other = rec.get("from") or {}
            else:
                other = (rec.get("to") or [{}])[0]
            number = normalize_phone(other.get("phoneNumber"))
            if not number or number not in phone_map:
                continue
            db.add(
                PhoneEvent(
                    org_id=cfg.org_id,
                    rc_id=f"sms:{rc_id}",
                    kind="sms",
                    direction=direction or "inbound",
                    other_number=number,
                    other_name=(other.get("name") or "")[:255] or None,
                    happened_at=_parse_when(rec.get("creationTime")),
                    text=(rec.get("subject") or "")[:2000] or None,
                )
            )
            stored += 1
        paging = data.get("paging") or {}
        if page >= int(paging.get("totalPages") or 1):
            break
        page += 1
    return stored


async def sync_org(db, cfg: RingCentralIntegration) -> dict:
    access = await ensure_access_token(db, cfg)
    if not cfg.own_numbers:
        cfg.own_numbers = await fetch_own_numbers(access)
    phone_map = await _crm_phone_map(db, cfg.org_id)
    for own in cfg.own_numbers or []:
        phone_map.pop(own, None)  # never match the account's own numbers
    if not phone_map:
        cfg.last_synced_at = utcnow()
        cfg.sync_error = None
        return {"calls_synced": 0, "sms_synced": 0}
    calls = await sync_calls(db, cfg, access, phone_map)
    sms = await sync_sms(db, cfg, access, phone_map)
    # Recompute relationship metrics for every person with stored phone
    # history — self-healing, covers events stored before this feature too.
    stored_numbers = {
        n
        for (n,) in await db.execute(
            select(PhoneEvent.other_number).where(PhoneEvent.org_id == cfg.org_id).distinct()
        )
    }
    person_ids = {
        pid
        for number, (etype, pid) in phone_map.items()
        if etype == "person" and number in stored_numbers
    }
    if person_ids:
        from app.services.interactions import update_person_aggregates

        await update_person_aggregates(db, cfg.org_id, person_ids)
    cfg.last_synced_at = utcnow()
    cfg.sync_error = None
    return {"calls_synced": calls, "sms_synced": sms}


async def run_sync_pass() -> None:
    async with SessionLocal() as db:
        configs = (await db.execute(select(RingCentralIntegration))).scalars().all()
        for cfg in configs:
            try:
                await sync_org(db, cfg)
                await db.commit()
            except RingCentralError as exc:
                cfg.sync_error = str(exc)[:500]
                log.warning("RingCentral sync failed: %s", exc)
                await db.commit()


async def sync_loop() -> None:
    while True:
        try:
            await run_sync_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("RingCentral sync pass failed")
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)
