"""Google Workspace integration: per-user OAuth, Contacts import, Calendar sync.

Auth model: the org registers its own OAuth client (client_id/secret from
Google Cloud console, stored org-level); each user then connects their own
Google account through the authorization-code flow with offline access, and
we keep a per-user refresh token. Google is never a login method for the CRM
itself.

V1 scope is read-only: contacts feed the existing import machinery (same
duplicate detection and commit path as CSV), and calendar events sync into a
local table matched to CRM records by attendee email.
"""

import asyncio
import hashlib
import hmac
import logging
import time
import uuid
from email.utils import getaddresses, parseaddr
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete, select

from app.config import settings
from app.db import SessionLocal
from app.models import (
    CalendarEvent,
    CalendarEventAttendee,
    EmailMessage,
    EmailParticipant,
    GoogleAccount,
    GoogleIntegration,
    Lead,
    Person,
    utcnow,
)

log = logging.getLogger("google")

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    GMAIL_SCOPE,
]
SYNC_INTERVAL_SECONDS = 1800
STATE_TTL_SECONDS = 600
EVENT_WINDOW_PAST_DAYS = 30
EVENT_WINDOW_FUTURE_DAYS = 90


class GoogleError(Exception):
    pass


def redirect_uri() -> str:
    return f"{settings.app_url.rstrip('/')}/api/v1/integrations/google/callback"


# ---- signed state (stateless CSRF protection for the OAuth round-trip) ----

def make_state(user_id: uuid.UUID) -> str:
    ts = str(int(time.time()))
    payload = f"{user_id}:{ts}"
    sig = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def check_state(state: str) -> uuid.UUID | None:
    try:
        user_part, ts, sig = state.rsplit(":", 2)
    except ValueError:
        return None
    payload = f"{user_part}:{ts}"
    expected = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    if time.time() - int(ts) > STATE_TTL_SECONDS:
        return None
    try:
        return uuid.UUID(user_part)
    except ValueError:
        return None


# ---- OAuth ----

def auth_url(client_id: str, user_id: uuid.UUID) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",  # guarantees a refresh_token on reconnect
        "state": make_state(user_id),
    }
    return f"{settings.google_auth_url}?{urlencode(params)}"


async def _post_form(url: str, data: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, data=data)
    except httpx.HTTPError as exc:
        raise GoogleError(f"Cannot reach Google: {exc}") from exc
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error_description") or resp.json().get("error")
        except Exception:
            detail = resp.text[:200]
        raise GoogleError(f"Google rejected the request ({resp.status_code}): {detail}")
    return resp.json()


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


async def _get_json(url: str, access_token: str, params: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                url, params=params, headers={"Authorization": f"Bearer {access_token}"}
            )
    except httpx.HTTPError as exc:
        raise GoogleError(f"Cannot reach Google: {exc}") from exc
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = (resp.json().get("error") or {}).get("message", "")[:200]
        except Exception:
            pass
        raise GoogleError(
            f"Google GET {url.split('?')[0]} failed ({resp.status_code})"
            + (f": {detail}" if detail else "")
        )
    return resp.json()


async def get_userinfo(access_token: str) -> dict:
    return await _get_json(settings.google_userinfo_url, access_token)


async def ensure_access_token(db, cfg: GoogleIntegration, account: GoogleAccount) -> str:
    """Returns a valid access token, refreshing (and persisting) if expired."""
    if account.access_token and account.access_expires_at:
        expires = account.access_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires > utcnow() + timedelta(minutes=2):
            return account.access_token
    tokens = await _post_form(
        settings.google_token_url,
        {
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "refresh_token": account.refresh_token,
            "grant_type": "refresh_token",
        },
    )
    account.access_token = tokens["access_token"]
    account.access_expires_at = utcnow() + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    await db.flush()
    return account.access_token


async def revoke(account: GoogleAccount) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(settings.google_revoke_url, params={"token": account.refresh_token})
    except httpx.HTTPError:
        pass  # best effort — we delete our copy regardless


# ---- Contacts ----

async def fetch_contacts(access_token: str) -> list[dict]:
    """All of the user's Google contacts, paginated."""
    out: list[dict] = []
    page_token = None
    while True:
        params = {
            "personFields": "names,emailAddresses,phoneNumbers,organizations,urls,addresses",
            "pageSize": 200,
        }
        if page_token:
            params["pageToken"] = page_token
        data = await _get_json(
            f"{settings.google_people_base}/v1/people/me/connections", access_token, params
        )
        out.extend(data.get("connections", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            return out


def contact_to_row(contact: dict) -> dict | None:
    """Map a People API person to the same row shape the CSV importer uses,
    so preview/dedupe/commit are shared."""
    names = (contact.get("names") or [{}])[0]
    emails = contact.get("emailAddresses") or []
    phones = contact.get("phoneNumbers") or []
    orgs = (contact.get("organizations") or [{}])[0]
    addresses = (contact.get("addresses") or [{}])[0]

    data: dict = {}
    if names.get("givenName"):
        data["first_name"] = names["givenName"]
    if names.get("familyName"):
        data["last_name"] = names["familyName"]
    if not data and not emails:
        return None  # nothing identifying

    for e in emails:
        kind = (e.get("type") or "").lower()
        key = "personal_email" if kind == "home" else "work_email"
        data.setdefault(key, e.get("value"))
    for p in phones:
        kind = (p.get("type") or "").lower()
        key = "mobile_phone" if kind == "mobile" else "work_phone"
        data.setdefault(key, p.get("value"))
    if orgs.get("name"):
        data["company_name"] = orgs["name"]
    if orgs.get("title"):
        data["title"] = orgs["title"]
    if addresses.get("streetAddress"):
        data["street"] = addresses["streetAddress"]
    if addresses.get("city"):
        data["city"] = addresses["city"]
    if addresses.get("region"):
        data["state"] = addresses["region"]
    if addresses.get("postalCode"):
        data["postal_code"] = addresses["postalCode"]
    if addresses.get("country"):
        data["country"] = addresses["country"]

    return {"data": data, "tags": ["Google Contacts"], "custom_fields": {}}


# ---- Calendar ----

def _parse_when(when: dict | None) -> tuple[datetime | None, bool]:
    if not when:
        return None, False
    if when.get("dateTime"):
        try:
            return datetime.fromisoformat(when["dateTime"].replace("Z", "+00:00")), False
        except ValueError:
            return None, False
    if when.get("date"):
        try:
            return datetime.fromisoformat(when["date"]).replace(tzinfo=timezone.utc), True
        except ValueError:
            return None, True
    return None, False


async def sync_calendar(db, cfg: GoogleIntegration, account: GoogleAccount) -> int:
    """Upsert the account's primary-calendar events for the sync window.
    Returns how many events were written."""
    access = await ensure_access_token(db, cfg, account)
    time_min = (utcnow() - timedelta(days=EVENT_WINDOW_PAST_DAYS)).isoformat()
    time_max = (utcnow() + timedelta(days=EVENT_WINDOW_FUTURE_DAYS)).isoformat()

    count = 0
    page_token = None
    while True:
        params = {
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "maxResults": 250,
            "orderBy": "startTime",
        }
        if page_token:
            params["pageToken"] = page_token
        data = await _get_json(
            f"{settings.google_calendar_base}/calendars/primary/events", access, params
        )
        for item in data.get("items", []):
            if item.get("status") == "cancelled":
                continue
            event_id = item.get("id")
            if not event_id:
                continue
            starts_at, all_day = _parse_when(item.get("start"))
            ends_at, _ = _parse_when(item.get("end"))
            existing = (
                await db.execute(
                    select(CalendarEvent).where(
                        CalendarEvent.org_id == account.org_id,
                        CalendarEvent.google_event_id == event_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                existing = CalendarEvent(org_id=account.org_id, google_event_id=event_id)
                db.add(existing)
            existing.owner_user_id = account.user_id
            existing.summary = (item.get("summary") or "")[:500] or None
            existing.location = (item.get("location") or "")[:500] or None
            existing.starts_at = starts_at
            existing.ends_at = ends_at
            existing.all_day = all_day
            existing.html_link = item.get("htmlLink")
            await db.flush()
            await db.execute(
                delete(CalendarEventAttendee).where(CalendarEventAttendee.event_id == existing.id)
            )
            seen: set[str] = set()
            for att in item.get("attendees", []) or []:
                email = (att.get("email") or "").lower().strip()
                if not email or email in seen:
                    continue
                seen.add(email)
                db.add(
                    CalendarEventAttendee(
                        event_id=existing.id,
                        email=email,
                        display_name=att.get("displayName"),
                    )
                )
            count += 1
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    account.last_synced_at = utcnow()
    account.sync_error = None
    return count




# ---- Gmail ----

def has_gmail_scope(account: GoogleAccount) -> bool:
    return GMAIL_SCOPE in (account.scopes or "")


async def _crm_email_map(db, org_id: uuid.UUID) -> dict[str, tuple[str, uuid.UUID]]:
    """Every known contact email in the org -> (entity_type, id)."""
    out: dict[str, tuple[str, uuid.UUID]] = {}
    rows = await db.execute(
        select(Person.id, Person.work_email, Person.personal_email).where(Person.org_id == org_id)
    )
    for pid, work, personal in rows:
        for e in (work, personal):
            if e:
                out[e.lower().strip()] = ("person", pid)
    rows = await db.execute(
        select(Lead.id, Lead.email).where(Lead.org_id == org_id, Lead.email.is_not(None))
    )
    for lid, e in rows:
        out.setdefault(e.lower().strip(), ("lead", lid))
    return out


def _parse_message(item: dict, owner_email: str) -> dict | None:
    """Gmail metadata payload -> storable fields + participant list."""
    headers = {
        h.get("name", "").lower(): h.get("value", "")
        for h in (item.get("payload", {}).get("headers") or [])
    }
    participants: list[tuple[str, str, str | None]] = []  # (kind, email, name)
    from_name, from_email = parseaddr(headers.get("from", ""))
    if from_email:
        participants.append(("from", from_email.lower().strip(), from_name or None))
    for kind, header in (("to", "to"), ("cc", "cc")):
        for name, addr in getaddresses([headers.get(header, "")]):
            if addr:
                participants.append((kind, addr.lower().strip(), name or None))
    if not participants:
        return None
    sent_at = None
    if item.get("internalDate"):
        try:
            sent_at = datetime.fromtimestamp(int(item["internalDate"]) / 1000, tz=timezone.utc)
        except (ValueError, OSError):
            pass
    return {
        "gmail_id": item.get("id"),
        "gmail_thread_id": item.get("threadId"),
        "rfc_message_id": (headers.get("message-id") or "").strip()[:255],
        "subject": (headers.get("subject") or "")[:500] or None,
        "snippet": (item.get("snippet") or "")[:500] or None,
        "from_email": from_email.lower().strip() if from_email else None,
        "from_name": from_name or None,
        "is_outgoing": "SENT" in (item.get("labelIds") or [])
        or (from_email or "").lower().strip() == owner_email,
        "sent_at": sent_at,
        "participants": participants,
    }


async def _fetch_message_meta(access: str, gmail_id: str) -> dict | None:
    try:
        return await _get_json(
            f"{settings.google_gmail_base}/users/me/messages/{gmail_id}",
            access,
            {
                "format": "metadata",
                "metadataHeaders": ["From", "To", "Cc", "Subject", "Message-ID"],
            },
        )
    except GoogleError as exc:
        if "(404)" in str(exc):
            return None  # message deleted between list and get
        raise


async def _known_gmail_ids(db, owner_user_id: uuid.UUID, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    rows = await db.execute(
        select(EmailMessage.gmail_id).where(
            EmailMessage.owner_user_id == owner_user_id, EmailMessage.gmail_id.in_(ids)
        )
    )
    return {gid for (gid,) in rows}


async def _store_messages(
    db, account: GoogleAccount, access: str, gmail_ids: list[str],
    contact_map: dict[str, tuple[str, uuid.UUID]],
) -> tuple[int, set[uuid.UUID]]:
    """Fetch metadata for unseen ids, keep only CRM-matching mail.
    Returns (stored_count, matched_person_ids)."""
    owner_email = account.email.lower().strip()
    stored = 0
    matched_people: set[uuid.UUID] = set()

    unseen = [g for g in gmail_ids if g]
    known = await _known_gmail_ids(db, account.user_id, unseen)
    unseen = [g for g in unseen if g not in known]

    CHUNK = 10
    for i in range(0, len(unseen), CHUNK):
        chunk = unseen[i : i + CHUNK]
        items = await asyncio.gather(*[_fetch_message_meta(access, g) for g in chunk])
        for item in items:
            if item is None:
                continue
            parsed = _parse_message(item, owner_email)
            if parsed is None:
                continue
            participants = parsed.pop("participants")
            matches = {
                contact_map[email]
                for _, email, _ in participants
                if email != owner_email and email in contact_map
            }
            if not matches:
                continue
            rfc = parsed["rfc_message_id"] or f"gmail:{account.user_id}:{parsed['gmail_id']}"
            duplicate = (
                await db.execute(
                    select(EmailMessage.id).where(
                        EmailMessage.org_id == account.org_id,
                        EmailMessage.rfc_message_id == rfc,
                    )
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                continue
            msg = EmailMessage(
                org_id=account.org_id, owner_user_id=account.user_id,
                **{**parsed, "rfc_message_id": rfc},
            )
            db.add(msg)
            await db.flush()
            seen_emails: set[str] = set()
            for kind, email, name in participants:
                if email in seen_emails:
                    continue
                seen_emails.add(email)
                db.add(
                    EmailParticipant(
                        email_id=msg.id, email=email, kind=kind,
                        display_name=(name or "")[:255] or None,
                    )
                )
            stored += 1
            matched_people.update(pid for etype, pid in matches if etype == "person")
    return stored, matched_people


async def _update_person_aggregates(db, org_id: uuid.UUID, person_ids: set[uuid.UUID]) -> None:
    """Copper-style columns: interactions = matched emails, last contacted =
    most recent one."""
    from sqlalchemy import func

    await db.flush()  # participant rows may still be pending (autoflush is off)

    for pid in person_ids:
        person = (
            await db.execute(select(Person).where(Person.id == pid, Person.org_id == org_id))
        ).scalar_one_or_none()
        if person is None:
            continue
        emails = [e.lower() for e in (person.work_email, person.personal_email) if e]
        if not emails:
            continue
        count, latest = (
            await db.execute(
                select(func.count(func.distinct(EmailMessage.id)), func.max(EmailMessage.sent_at))
                .join(EmailParticipant, EmailParticipant.email_id == EmailMessage.id)
                .where(EmailMessage.org_id == org_id, EmailParticipant.email.in_(emails))
            )
        ).one()
        person.interaction_count = count or 0
        if latest is not None:
            person.last_contacted_at = latest


ADDRESSES_PER_QUERY = 15


async def _search_contact_mail(access: str, addresses: list[str]) -> list[str]:
    """Ask Gmail for mail exchanged with the given addresses, rather than
    crawling the whole mailbox — one search per 15 contacts, so it is fast and
    quota-cheap even on large accounts."""
    ids: list[str] = []
    seen: set[str] = set()
    for i in range(0, len(addresses), ADDRESSES_PER_QUERY):
        chunk = addresses[i : i + ADDRESSES_PER_QUERY]
        clause = " OR ".join(f"from:{a} OR to:{a} OR cc:{a}" for a in chunk)
        q = f"newer_than:{settings.gmail_backfill_days}d -in:spam -in:trash ({clause})"
        page_token = None
        while True:
            params = {"q": q, "maxResults": 500}
            if page_token:
                params["pageToken"] = page_token
            data = await _get_json(
                f"{settings.google_gmail_base}/users/me/messages", access, params
            )
            for m in data.get("messages", []):
                mid = m.get("id")
                if mid and mid not in seen:
                    seen.add(mid)
                    ids.append(mid)
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    return ids


async def sync_gmail(
    db, cfg: GoogleIntegration, account: GoogleAccount, force_backfill: bool = False
) -> int:
    """Targeted backfill (searches the window for known contacts), then the
    history feed for new mail. Only mail involving known People/Leads is
    stored. force_backfill re-runs the search — how newly added contacts get
    their history pulled."""
    access = await ensure_access_token(db, cfg, account)
    contact_map = await _crm_email_map(db, account.org_id)
    if not contact_map:
        return 0
    stored_total = 0
    matched_people: set[uuid.UUID] = set()

    if force_backfill or not account.gmail_backfill_done:
        # Snapshot the cursor FIRST so mail arriving mid-backfill isn't missed.
        profile = await _get_json(f"{settings.google_gmail_base}/users/me/profile", access)
        ids = await _search_contact_mail(access, list(contact_map.keys()))
        stored, people = await _store_messages(db, account, access, ids, contact_map)
        stored_total += stored
        matched_people |= people
        if not account.gmail_history_id:
            account.gmail_history_id = str(profile.get("historyId") or "")
        account.gmail_backfill_done = True
    elif account.gmail_history_id:
        ids = []
        page_token = None
        newest_history = account.gmail_history_id
        try:
            while True:
                params = {
                    "startHistoryId": account.gmail_history_id,
                    "historyTypes": "messageAdded",
                    "maxResults": 500,
                }
                if page_token:
                    params["pageToken"] = page_token
                data = await _get_json(
                    f"{settings.google_gmail_base}/users/me/history", access, params
                )
                for h in data.get("history", []):
                    for added in h.get("messagesAdded", []):
                        mid = (added.get("message") or {}).get("id")
                        if mid:
                            ids.append(mid)
                newest_history = str(data.get("historyId") or newest_history)
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
        except GoogleError as exc:
            if "(404)" in str(exc):
                # Cursor expired — re-run the backfill window; dedupe makes it cheap.
                account.gmail_backfill_done = False
                account.gmail_history_id = None
                return stored_total
            raise
        stored, people = await _store_messages(db, account, access, ids, contact_map)
        stored_total += stored
        matched_people |= people
        account.gmail_history_id = newest_history

    if matched_people:
        await _update_person_aggregates(db, account.org_id, matched_people)
    return stored_total


async def sync_account(db, account: GoogleAccount, force_backfill: bool = False) -> dict:
    cfg = (
        await db.execute(
            select(GoogleIntegration).where(GoogleIntegration.org_id == account.org_id)
        )
    ).scalar_one_or_none()
    if cfg is None:
        raise GoogleError("Google integration is not configured")
    events = await sync_calendar(db, cfg, account)
    emails = (
        await sync_gmail(db, cfg, account, force_backfill=force_backfill)
        if has_gmail_scope(account)
        else 0
    )
    return {"events_synced": events, "emails_synced": emails}


async def run_sync_pass() -> None:
    async with SessionLocal() as db:
        accounts = (await db.execute(select(GoogleAccount))).scalars().all()
        for account in accounts:
            try:
                await sync_account(db, account)
                await db.commit()
            except GoogleError as exc:
                account.sync_error = str(exc)[:500]
                log.warning("Google sync failed for %s: %s", account.email, exc)
                await db.commit()


async def sync_loop() -> None:
    while True:
        try:
            await run_sync_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Google sync pass failed")
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)
