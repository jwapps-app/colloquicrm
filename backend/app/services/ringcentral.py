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


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    # One keep-alive pool for the whole sync instead of a TLS handshake per
    # page. Auth and headers are per call; nothing is baked into the client.
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=25.0, limits=httpx.Limits(max_connections=10, max_keepalive_connections=5)
        )
    return _client


async def aclose() -> None:
    """Drain the shared pool; called once from app shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _post_form(url: str, data: dict, auth: tuple[str, str]) -> dict:
    try:
        resp = await _get_client().post(url, data=data, auth=auth, timeout=15.0)
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
        resp = await _get_client().get(
            url, params=params, headers={"Authorization": f"Bearer {access}"}, timeout=25.0
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


async def _release_db(db) -> None:
    """End the session's transaction before waiting on RingCentral — the
    same rule the Google sync follows. A call-log walk is many round-trips;
    none of them may pin a pooled connection or hold a snapshot open. What
    this commits is only finished work: whole pages, deduped on rc_id if the
    walk is repeated."""
    if db.in_transaction():
        await db.commit()


async def request_token(client_id: str, client_secret: str, jwt: str) -> dict:
    """The JWT-bearer token exchange. Pure network — takes the credentials
    themselves so the connect route can validate them before storing any."""
    tokens = await _post_form(
        f"{settings.ringcentral_base}/restapi/oauth/token",
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": jwt,
        },
        auth=(client_id, client_secret),
    )
    if not tokens.get("access_token"):
        raise RingCentralError("RingCentral returned no access token")
    return tokens


def token_expiry(tokens: dict) -> datetime:
    return utcnow() + timedelta(seconds=int(tokens.get("expires_in", 3600)))


async def ensure_access_token(db, cfg: RingCentralIntegration) -> str:
    if cfg.access_token and cfg.access_expires_at:
        expires = cfg.access_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires > utcnow() + timedelta(minutes=2):
            return cfg.access_token
    await _release_db(db)  # the token round-trip must not sit inside a transaction
    tokens = await request_token(cfg.client_id, cfg.client_secret, cfg.jwt)
    cfg.access_token = tokens["access_token"]
    cfg.access_expires_at = token_expiry(tokens)
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
        select(Person.id, Person.work_phone, Person.mobile_phone).where(
            Person.org_id == org_id, Person.deleted_at.is_(None)
        )
    )
    for pid, work, mobile in rows:
        for p in (work, mobile):
            n = normalize_phone(p)
            if n:
                out[n] = ("person", pid)
    rows = await db.execute(
        select(Lead.id, Lead.work_phone, Lead.mobile_phone).where(
            Lead.org_id == org_id, Lead.deleted_at.is_(None)
        )
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


def _sync_window_start(cfg: RingCentralIntegration) -> str:
    """dateFrom for a sync pass. First sync walks the whole backfill window;
    after that, resume from the last pass minus a 24h overlap so late-written
    records aren't missed — the rc_id dedup absorbs the repeats."""
    last = cfg.last_synced_at
    if last is None:
        return (utcnow() - timedelta(days=settings.ringcentral_backfill_days)).isoformat()
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (last - timedelta(hours=24)).isoformat()


async def _walk_pages(db, access: str, url: str, params: dict, store_page) -> int:
    """Fetch a page, write a page. The fetch happens with no transaction
    open; the write for one page (at most 250 records, plus the metrics of
    the people it touched) is committed before the next page is requested."""
    stored = 0
    page = 1
    while True:
        await _release_db(db)
        data = await _get_json(url, access, {**params, "page": page})
        stored += await store_page(data.get("records", []))
        paging = data.get("paging") or {}
        if page >= int(paging.get("totalPages") or 1):
            break
        page += 1
    return stored


async def _recompute_touched(db, cfg: RingCentralIntegration, phone_map: dict, touched: set[str]) -> None:
    # Only people with NEW events on this page — recomputing everyone with
    # any history burned thousands of queries every cycle once real call logs
    # existed. Same transaction as the rows, so a committed page never leaves
    # a count behind it.
    person_ids = {
        phone_map[n][1] for n in touched if n in phone_map and phone_map[n][0] == "person"
    }
    if person_ids:
        from app.services.interactions import update_person_aggregates

        await update_person_aggregates(db, cfg.org_id, person_ids)


async def sync_calls(
    db, cfg: RingCentralIntegration, access: str, phone_map: dict, date_from: str
) -> int:
    async def store_page(records: list[dict]) -> int:
        stored = 0
        touched: set[str] = set()
        known = await _known_rc_ids(
            db, cfg.org_id, [f"call:{r.get('id')}" for r in records if r.get("id")]
        )
        for rec in records:
            rc_id = rec.get("id")
            if not rc_id or f"call:{rc_id}" in known:
                continue
            known.add(f"call:{rc_id}")  # a record repeated within the page
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
            touched.add(number)
            stored += 1
        await _recompute_touched(db, cfg, phone_map, touched)
        return stored

    return await _walk_pages(
        db, access,
        f"{settings.ringcentral_base}/restapi/v1.0/account/~/call-log",
        {"view": "Simple", "perPage": 250, "dateFrom": date_from},
        store_page,
    )


async def sync_sms(
    db, cfg: RingCentralIntegration, access: str, phone_map: dict, date_from: str
) -> int:
    async def store_page(records: list[dict]) -> int:
        stored = 0
        touched: set[str] = set()
        known = await _known_rc_ids(
            db, cfg.org_id, [f"sms:{r.get('id')}" for r in records if r.get("id")]
        )
        for rec in records:
            rc_id = rec.get("id")
            if not rc_id or f"sms:{rc_id}" in known:
                continue
            known.add(f"sms:{rc_id}")
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
            touched.add(number)
            stored += 1
        await _recompute_touched(db, cfg, phone_map, touched)
        return stored

    return await _walk_pages(
        db, access,
        f"{settings.ringcentral_base}/restapi/v1.0/account/~/extension/~/message-store",
        {"messageType": "SMS", "perPage": 250, "dateFrom": date_from},
        store_page,
    )


async def sync_org(db, cfg: RingCentralIntegration) -> dict:
    """No transaction is held across any RingCentral round-trip: every fetch
    sits behind _release_db, and writes land a page at a time. An interrupted
    walk leaves whole pages behind and last_synced_at untouched, so the next
    pass covers the same window and the rc_id dedupe absorbs the repeats."""
    access = await ensure_access_token(db, cfg)
    if not cfg.own_numbers:
        await _release_db(db)
        cfg.own_numbers = await fetch_own_numbers(access)
    phone_map = await _crm_phone_map(db, cfg.org_id)
    for own in cfg.own_numbers or []:
        phone_map.pop(own, None)  # never match the account's own numbers
    if not phone_map:
        cfg.last_synced_at = utcnow()
        cfg.sync_error = None
        return {"calls_synced": 0, "sms_synced": 0}
    date_from = _sync_window_start(cfg)
    started = utcnow()
    calls = await sync_calls(db, cfg, access, phone_map, date_from)
    sms = await sync_sms(db, cfg, access, phone_map, date_from)
    # Stamped with when the walk STARTED: a long walk must not push the next
    # window's start past records written while it was running.
    cfg.last_synced_at = started
    cfg.sync_error = None
    return {"calls_synced": calls, "sms_synced": sms}


def _sync_error_text(exc: Exception) -> str:
    # RingCentralError messages are already curated; anything else gets its
    # type named so the status is never a mystery.
    if isinstance(exc, RingCentralError):
        return str(exc)[:500]
    return f"Sync failed: {type(exc).__name__}: {exc}"[:500]


# ---- one sync at a time per org ----
#
# The manual "Sync now" and the scheduled pass would otherwise insert the
# same rc_ids concurrently and trip the unique constraint. Single process, so
# an in-process claim does it; taken synchronously, so it cannot race.

_syncing: set[uuid.UUID] = set()


def try_claim_sync(org_id: uuid.UUID) -> bool:
    if org_id in _syncing:
        return False
    _syncing.add(org_id)
    return True


def release_sync(org_id: uuid.UUID) -> None:
    _syncing.discard(org_id)


async def _record_sync_error(org_id: uuid.UUID, exc: Exception) -> None:
    # A session of its own — nothing here leans on the failed one.
    async with SessionLocal() as db:
        cfg = (
            await db.execute(
                select(RingCentralIntegration).where(RingCentralIntegration.org_id == org_id)
            )
        ).scalar_one_or_none()
        if cfg is not None:
            cfg.sync_error = _sync_error_text(exc)
            await db.commit()


async def run_sync_pass() -> None:
    # Plain ids, and a fresh session per org: a failure (and its rollback)
    # in one org leaves nothing expired for the next iteration to trip on.
    async with SessionLocal() as db:
        org_ids = [
            oid for (oid,) in await db.execute(select(RingCentralIntegration.org_id))
        ]
    for org_id in org_ids:
        if not try_claim_sync(org_id):
            log.info("RingCentral sync for org %s already running; skipping this pass", org_id)
            continue
        try:
            async with SessionLocal() as db:
                cfg = (
                    await db.execute(
                        select(RingCentralIntegration).where(
                            RingCentralIntegration.org_id == org_id
                        )
                    )
                ).scalar_one_or_none()
                if cfg is None:
                    continue
                await sync_org(db, cfg)
                await db.commit()
        except Exception as exc:
            # Record ANY failure, not just RingCentralError — an unrecorded
            # crash leaves a stale status, and one broken org must not stop
            # the pass for the rest.
            log.warning("RingCentral sync failed for org %s: %s", org_id, exc)
            try:
                await _record_sync_error(org_id, exc)
            except Exception:
                log.exception("could not record the RingCentral sync error for org %s", org_id)
        finally:
            release_sync(org_id)


async def sync_loop() -> None:
    while True:
        try:
            await run_sync_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("RingCentral sync pass failed")
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)
