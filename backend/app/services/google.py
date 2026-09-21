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
import base64
import bisect
import hashlib
import hmac
import logging
import time
import uuid
from email.utils import getaddresses, parseaddr
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete, func, select, update

from app.config import settings
from app.db import SessionLocal
from app.models import (
    CalendarEvent,
    CalendarEventAttendee,
    ContactSuggestion,
    EmailMessage,
    EmailParticipant,
    GmailSyncFailure,
    GoogleAccount,
    GoogleIntegration,
    Lead,
    Person,
    User,
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
# A backfill pass is bounded (BACKFILL_MAX_IDS_PER_PASS); while any mailbox
# still has history left to walk, the loop comes back sooner than the regular
# interval so "all history" finishes in hours, not weeks.
BACKFILL_CONTINUE_SECONDS = 60
# Ceiling on the back-to-back passes one manual sync will take (each lists up
# to BACKFILL_MAX_IDS_PER_PASS ids); the scheduled loop finishes anything left.
MANUAL_SYNC_MAX_PASSES = 250
STATE_TTL_SECONDS = 600
EVENT_WINDOW_PAST_DAYS = 30
EVENT_WINDOW_FUTURE_DAYS = 90


class GoogleError(Exception):
    """status_code is Google's HTTP status when it answered with an error,
    None when it could not be reached at all."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


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


# ---- HTTP ----

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    # One keep-alive pool for every Google call. A backfill makes thousands
    # of requests and each fresh AsyncClient was a TLS handshake; the pool is
    # sized for the ten-wide message fetches. Per-call timeouts and the
    # bearer header are passed per request — nothing is baked in.
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


async def aclose() -> None:
    """Drain the shared pool; called once from app shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _release_db(db) -> None:
    """End the session's transaction before waiting on Google.

    Every network call in the sync path sits behind one of these: the
    connection goes back to the pool and Postgres holds no snapshot while a
    page or a message batch is in flight (a backfill runs for minutes, and an
    idle-in-transaction session that long blocks vacuum and pins a pooled
    connection). What it commits is only finished work — rows the next
    checkpoint would have kept anyway, deduped on resume by gmail_id and
    rfc_message_id."""
    if db.in_transaction():
        await db.commit()


# ---- OAuth ----

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


async def _post_form(url: str, data: dict) -> dict:
    # This is the OAuth token endpoint — it runs at the START of every sync to
    # refresh the access token, so a transient blip here aborts the whole pass
    # before anything else. Retry with backoff; a real rejection (4xx) below is
    # not retried.
    resp = None
    for attempt in range(3):
        try:
            resp = await _get_client().post(url, data=data, timeout=30.0)
            break
        except httpx.HTTPError as exc:
            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise GoogleError(
                f"Cannot reach Google: {type(exc).__name__}: {exc}".rstrip(": ")
            ) from exc
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error_description") or resp.json().get("error")
        except Exception:
            detail = resp.text[:200]
        raise GoogleError(
            f"Google rejected the request ({resp.status_code}): {detail}",
            status_code=resp.status_code,
        )
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


async def _get_json(
    url: str, access_token: str, params: dict | None = None, timeout: float = 20.0
) -> dict:
    # Transport errors (timeouts, dropped connections) are transient — retry a
    # couple times with backoff before giving up. Gmail 4xx/5xx are handled
    # below and NOT retried here, so rate limits still surface as GoogleError.
    resp = None
    for attempt in range(3):
        try:
            resp = await _get_client().get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=timeout,
            )
            break
        except httpx.HTTPError as exc:
            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            # str(timeout errors) is often empty — name the class so the
            # sync_error is legible.
            raise GoogleError(
                f"Cannot reach Google: {type(exc).__name__}: {exc}".rstrip(": ")
            ) from exc
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = (resp.json().get("error") or {}).get("message", "")[:200]
        except Exception:
            pass
        raise GoogleError(
            f"Google GET {url.split('?')[0]} failed ({resp.status_code})"
            + (f": {detail}" if detail else ""),
            status_code=resp.status_code,
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
    await _release_db(db)  # the refresh round-trip must not sit inside a transaction
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
        await _get_client().post(
            settings.google_revoke_url, params={"token": account.refresh_token}, timeout=10.0
        )
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
        # An all-day event is a calendar date, not an instant, but the column
        # (and both clients) deal in timestamps rendered in the viewer's own
        # zone. Midnight UTC shows up as the previous day anywhere west of
        # Greenwich; 12:00 UTC lands on the right date for every zone from
        # UTC-11 to UTC+11 with no client change. Existing rows were moved by
        # the same +12h in migration b4e1d7a90c35.
        try:
            day = datetime.fromisoformat(when["date"])
            return day.replace(hour=12, minute=0, second=0, microsecond=0, tzinfo=timezone.utc), True
        except ValueError:
            return None, True
    return None, False


async def _delete_calendar_events(db, event_ids: list[uuid.UUID]) -> None:
    # Attendees first: the FK cascades on Postgres, but SQLite dev doesn't
    # enforce it.
    for i in range(0, len(event_ids), 5000):
        chunk = event_ids[i : i + 5000]
        await db.execute(
            delete(CalendarEventAttendee).where(CalendarEventAttendee.event_id.in_(chunk))
        )
        await db.execute(delete(CalendarEvent).where(CalendarEvent.id.in_(chunk)))


async def _store_calendar_page(
    db, account: GoogleAccount, items: list[dict], seen: set[str] | None = None
) -> int:
    """Upsert one page of events (pure DB work — no network in here). Live
    event ids are added to `seen` so the caller can reconcile the window."""
    # One lookup for the whole page instead of a SELECT per event.
    page_ids = [i.get("id") for i in items if i.get("id")]
    existing_by_gid: dict[str, CalendarEvent] = {}
    if page_ids:
        rows = await db.execute(
            select(CalendarEvent).where(
                CalendarEvent.org_id == account.org_id,
                CalendarEvent.google_event_id.in_(page_ids),
            )
        )
        existing_by_gid = {e.google_event_id: e for e in rows.scalars()}
    count = 0
    cancelled: list[CalendarEvent] = []
    for item in items:
        event_id = item.get("id")
        if not event_id:
            continue
        if item.get("status") == "cancelled":
            # A tombstone: drop our copy. Rows are shared org-wide by Google
            # event id, so only the copy this mailbox wrote goes — a colleague
            # who still has the meeting keeps theirs.
            stale = existing_by_gid.pop(event_id, None)
            if stale is not None and stale.owner_user_id in (None, account.user_id):
                cancelled.append(stale)
            continue
        if seen is not None:
            seen.add(event_id)
        starts_at, all_day = _parse_when(item.get("start"))
        ends_at, _ = _parse_when(item.get("end"))
        existing = existing_by_gid.get(event_id)
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
        seen_attendees: set[str] = set()
        for att in item.get("attendees", []) or []:
            # Stored normalized (gmail dots/+suffixes stripped) — the
            # entity-matching queries look attendees up by normalize_email.
            email = normalize_email(att.get("email") or "")
            if not email or email in seen_attendees:
                continue
            seen_attendees.add(email)
            db.add(
                CalendarEventAttendee(
                    event_id=existing.id,
                    email=email,
                    display_name=att.get("displayName"),
                )
            )
        count += 1
    if cancelled:
        await db.execute(
            delete(CalendarEventAttendee).where(
                CalendarEventAttendee.event_id.in_([e.id for e in cancelled])
            )
        )
        for stale in cancelled:
            await db.delete(stale)
        await db.flush()
    return count


# The reconciliation leaves a day's margin inside each edge of the fetched
# window: an all-day event is stored at noon UTC, up to ~14h away from its
# real start, and must not be judged "missing" by that skew at the boundary.
RECONCILE_EDGE_MARGIN = timedelta(days=1)


async def _reconcile_calendar_window(
    db, account: GoogleAccount, window_start: datetime, window_end: datetime, seen: set[str]
) -> int:
    """Remove this mailbox's stored events that start inside the window just
    walked but were absent from the complete result set — deleted, cancelled
    (Google omits those from a plain listing), or moved out of range. Scoped
    to rows this account wrote; events outside the window are history and
    stay."""
    rows = await db.execute(
        select(CalendarEvent.id, CalendarEvent.google_event_id).where(
            CalendarEvent.org_id == account.org_id,
            CalendarEvent.owner_user_id == account.user_id,
            CalendarEvent.starts_at >= window_start + RECONCILE_EDGE_MARGIN,
            CalendarEvent.starts_at < window_end - RECONCILE_EDGE_MARGIN,
        )
    )
    gone = [eid for eid, gid in rows if gid not in seen]
    if gone:
        await _delete_calendar_events(db, gone)
        log.info("calendar reconcile for %s removed %s vanished events", account.user_id, len(gone))
    return len(gone)


async def sync_calendar(db, cfg: GoogleIntegration, account: GoogleAccount) -> int:
    """Upsert the account's primary-calendar events for the sync window.
    Returns how many events were written. Fetch a page, write a page: the
    write for one page is committed before the next page is requested, so
    no transaction ever spans a Calendar round-trip."""
    access = await ensure_access_token(db, cfg, account)
    window_start = utcnow() - timedelta(days=EVENT_WINDOW_PAST_DAYS)
    window_end = utcnow() + timedelta(days=EVENT_WINDOW_FUTURE_DAYS)
    time_min = window_start.isoformat()
    time_max = window_end.isoformat()

    count = 0
    seen: set[str] = set()
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
        await _release_db(db)
        data = await _get_json(
            f"{settings.google_calendar_base}/calendars/primary/events", access, params
        )
        count += await _store_calendar_page(db, account, data.get("items", []), seen)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    # Only reached when every page came back — a failed walk raises above and
    # never gets to call anything "missing".
    await _reconcile_calendar_window(db, account, window_start, window_end, seen)
    account.last_synced_at = utcnow()
    account.sync_error = None
    return count




# ---- Gmail ----

def has_gmail_scope(account: GoogleAccount) -> bool:
    return GMAIL_SCOPE in (account.scopes or "")


def normalize_email(addr: str) -> str:
    """Canonical form for matching. Gmail ignores dots and +suffixes in the
    local part, so `m.r.w+x@gmail.com` and `mrw@googlemail.com` are the same
    inbox — match them as one."""
    addr = (addr or "").lower().strip()
    local, _, domain = addr.partition("@")
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
        return f"{local}@gmail.com"
    return addr


async def _crm_email_map(db, org_id: uuid.UUID) -> dict[str, tuple[str, uuid.UUID]]:
    """Every known contact email in the org -> (entity_type, id)."""
    out: dict[str, tuple[str, uuid.UUID]] = {}
    rows = await db.execute(
        select(Person.id, Person.work_email, Person.personal_email).where(
            Person.org_id == org_id, Person.deleted_at.is_(None)
        )
    )
    for pid, work, personal in rows:
        for e in (work, personal):
            if e:
                out[normalize_email(e)] = ("person", pid)
    rows = await db.execute(
        select(Lead.id, Lead.email).where(
            Lead.org_id == org_id, Lead.email.is_not(None), Lead.deleted_at.is_(None)
        )
    )
    for lid, e in rows:
        out.setdefault(normalize_email(e), ("lead", lid))
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
        participants.append(("from", normalize_email(from_email), from_name or None))
    for kind, header in (("to", "to"), ("cc", "cc")):
        for name, addr in getaddresses([headers.get(header, "")]):
            if addr:
                participants.append((kind, normalize_email(addr), name or None))
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
        or normalize_email(from_email or "") == owner_email,
        "sent_at": sent_at,
        "participants": participants,
    }


# A single Gmail messages.get costs the same quota (5 units) at format=full as
# at format=metadata, so when archiving bodies we fetch full once and get the
# headers AND body from the one call — no extra API cost or rate-limit pressure.
MAX_BODY_CHARS = 1_000_000  # guard against a pathological newsletter row


async def _fetch_message(access: str, gmail_id: str, full: bool) -> dict | None:
    params = (
        {"format": "full"}
        if full
        else {
            "format": "metadata",
            "metadataHeaders": ["From", "To", "Cc", "Subject", "Message-ID"],
        }
    )
    try:
        return await _get_json(
            f"{settings.google_gmail_base}/users/me/messages/{gmail_id}", access, params,
            # Full bodies can be large — give the fetch room like the search has.
            timeout=SEARCH_TIMEOUT if full else 20.0,
        )
    except GoogleError as exc:
        if exc.status_code == 404:
            return None  # message deleted between list and get
        raise


async def _known_gmail_ids(db, owner_user_id: uuid.UUID, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    # A single prolific address can return tens of thousands of message ids;
    # asyncpg caps bind parameters at 32767, so chunk the IN() clause.
    known: set[str] = set()
    for i in range(0, len(ids), 5000):
        rows = await db.execute(
            select(EmailMessage.gmail_id).where(
                EmailMessage.owner_user_id == owner_user_id,
                EmailMessage.gmail_id.in_(ids[i : i + 5000]),
            )
        )
        known.update(gid for (gid,) in rows)
    return known


# Failed work is retried on later passes, this many times in total, before it
# is recorded as given up (kept, so the gap stays visible in the status).
MAX_FAILURE_ATTEMPTS = 5
# A queue this deep means Gmail itself is failing, not a few odd messages —
# hold the checkpoint instead of writing the whole mailbox into the queue.
MAX_PENDING_MESSAGE_FAILURES = 2000
# Fetched messages are written in groups: one transaction carries the rows,
# the retry-queue changes, and ONE interaction recompute for everyone the
# group touched. Bounded by count and by body size so a run of huge messages
# can't balloon memory between writes.
WRITE_GROUP_MESSAGES = 50
WRITE_GROUP_BODY_CHARS = 8_000_000


async def _failure_rows(db, user_id: uuid.UUID, kind: str) -> dict[str, GmailSyncFailure]:
    rows = await db.execute(
        select(GmailSyncFailure).where(
            GmailSyncFailure.user_id == user_id, GmailSyncFailure.kind == kind
        )
    )
    return {r.key: r for r in rows.scalars()}


def _note_failure(
    db, rows: dict[str, GmailSyncFailure], user_id: uuid.UUID, kind: str, key: str, exc: Exception
) -> bool:
    """Count one more failed attempt at `key`. True once it has been given up."""
    row = rows.get(key)
    if row is None:
        row = GmailSyncFailure(user_id=user_id, kind=kind, key=key, attempts=0, given_up=False)
        db.add(row)
        rows[key] = row
    row.attempts = (row.attempts or 0) + 1
    row.last_error = str(exc)[:300] or type(exc).__name__
    if row.attempts >= MAX_FAILURE_ATTEMPTS and not row.given_up:
        row.given_up = True
        log.warning(
            "giving up on gmail %s %s for %s after %s attempts: %s",
            kind, key, user_id, row.attempts, row.last_error,
        )
    return bool(row.given_up)


async def _clear_failures(
    db, rows: dict[str, GmailSyncFailure], keys
) -> None:
    for key in keys:
        row = rows.pop(key, None)
        if row is not None:
            await db.delete(row)


async def failure_counts(db, user_id: uuid.UUID) -> dict[str, int]:
    """Retry-queue sizes for the status endpoint."""
    out = {
        "gmail_messages_pending_retry": 0,
        "gmail_messages_given_up": 0,
        "gmail_addresses_pending_retry": 0,
        "gmail_addresses_given_up": 0,
    }
    rows = await db.execute(
        select(GmailSyncFailure.kind, GmailSyncFailure.given_up, func.count())
        .where(GmailSyncFailure.user_id == user_id)
        .group_by(GmailSyncFailure.kind, GmailSyncFailure.given_up)
    )
    for kind, given_up, n in rows:
        noun = "messages" if kind == "message" else "addresses"
        out[f"gmail_{noun}_{'given_up' if given_up else 'pending_retry'}"] = n
    return out


async def reset_mailbox_state(db, account: GoogleAccount) -> None:
    """Forget everything that describes a particular mailbox's sync position.
    Used when the connected Google identity changes: the history id, walk
    position and retry queue all belong to the old mailbox and would make the
    new one skip its history. Archived messages are left alone."""
    account.gmail_history_id = None
    account.gmail_backfill_done = False
    account.gmail_backfill_cursor = 0
    account.gmail_backfill_total = None
    account.gmail_backfill_after = None
    account.gmail_backfill_page_token = None
    account.last_synced_at = None
    await db.execute(delete(GmailSyncFailure).where(GmailSyncFailure.user_id == account.user_id))


async def _store_messages(
    db, account: GoogleAccount, access: str, gmail_ids: list[str],
    contact_map: dict[str, tuple[str, uuid.UUID]],
    attempted: set[str] | None = None,
) -> tuple[int, set[uuid.UUID]]:
    """Fetch metadata for unseen ids, keep only CRM-matching mail.
    Returns (stored_count, matched_person_ids).

    Fetches run ten at a time with no transaction open; what they return is
    parsed straight away and written in groups (WRITE_GROUP_*). One group is
    one transaction: the message rows, the retry-queue bookkeeping, and a
    single interaction recompute for every person the group touched — so a
    committed group never leaves a count behind its rows. An interruption
    anywhere leaves only whole groups; the caller's checkpoint stays put, the
    next pass re-lists the same ids, and the dedupe skips what landed.

    A message that fails to fetch (not a rate limit) is NOT dropped: it goes
    into gmail_sync_failures in the same transaction, which is what lets the
    caller advance its checkpoint past it. `attempted` carries the ids that
    already failed during this pass so a re-listing doesn't burn a second
    attempt on them."""
    owner_email = normalize_email(account.email)
    archive = settings.gmail_archive_bodies
    stored = 0
    matched_people: set[uuid.UUID] = set()
    if attempted is None:
        attempted = set()

    failures = await _failure_rows(db, account.user_id, "message")
    unseen = list(dict.fromkeys(g for g in gmail_ids if g))
    known = await _known_gmail_ids(db, account.user_id, unseen)
    # Already archived by another route: nothing left to retry.
    await _clear_failures(db, failures, [g for g in unseen if g in known])
    unseen = [
        g for g in unseen
        if g not in known
        and g not in attempted
        and not (g in failures and failures[g].given_up)
    ]

    pending: list[tuple[dict, list, set, str | None, str | None]] = []
    pending_chars = 0
    fetched_ok: list[str] = []
    fetch_failed: list[tuple[str, GoogleError]] = []

    async def write_group() -> None:
        nonlocal stored, pending_chars
        group_people: set[uuid.UUID] = set()
        rfcs = [
            parsed["rfc_message_id"] or f"gmail:{account.user_id}:{parsed['gmail_id']}"
            for parsed, *_ in pending
        ]
        taken: set[str] = set()
        if rfcs:
            rows = await db.execute(
                select(EmailMessage.rfc_message_id).where(
                    EmailMessage.org_id == account.org_id,
                    EmailMessage.rfc_message_id.in_(rfcs),
                )
            )
            taken = {r for (r,) in rows}
        for (parsed, participants, matches, text, html), rfc in zip(pending, rfcs):
            if rfc in taken:
                continue  # same message already synced (another mailbox, or a Sent/Inbox pair)
            taken.add(rfc)
            msg = EmailMessage(
                org_id=account.org_id, owner_user_id=account.user_id,
                **{**parsed, "rfc_message_id": rfc},
            )
            if archive:
                # Body came free with the full fetch — store it now so the CRM
                # keeps the message even if it's later deleted from Gmail.
                msg.body_text = text
                msg.body_html = html
                msg.body_fetched_at = utcnow()
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
                        direct=kind == "from" or bool(parsed["is_outgoing"]),
                        display_name=(name or "")[:255] or None,
                    )
                )
            stored += 1
            group_people.update(pid for etype, pid in matches if etype == "person")
        # Every id that fetched is now classified — stored, a duplicate, or
        # not CRM mail — so any retry owed for it is settled.
        await _clear_failures(db, failures, fetched_ok)
        for gid, exc in fetch_failed:
            _note_failure(db, failures, account.user_id, "message", gid, exc)
            attempted.add(gid)
        # Metrics ride in the same transaction as the rows they count — once
        # for the whole group, not once per ten messages.
        if group_people:
            await _update_person_aggregates(db, account.org_id, group_people)
            matched_people.update(group_people)
        await db.flush()
        pending.clear()
        fetched_ok.clear()
        fetch_failed.clear()
        pending_chars = 0

    CHUNK = 10
    for i in range(0, len(unseen), CHUNK):
        chunk = unseen[i : i + CHUNK]
        await _release_db(db)
        results = await asyncio.gather(
            *[_fetch_message(access, g, archive) for g in chunk], return_exceptions=True
        )
        rate_limited: GoogleError | None = None
        for gid, res in zip(chunk, results):
            if isinstance(res, GoogleError):
                if _is_rate_limit(res):
                    rate_limited = res  # not a failed attempt: simply not done yet
                    continue
                # A single message that won't fetch (usually a timeout on a
                # large full-body message) must not abort the whole chunk, and
                # must not vanish either: it is queued for retry when this
                # group is written.
                log.warning("message %s failed to fetch, queued for retry: %s", gid, res)
                fetch_failed.append((gid, res))
                continue
            if isinstance(res, BaseException):
                raise res  # unexpected: don't silently swallow
            fetched_ok.append(gid)
            if res is None:
                continue  # deleted between list and get
            parsed = _parse_message(res, owner_email)
            if parsed is None:
                continue
            participants = parsed.pop("participants")
            # An interaction requires engagement: the contact wrote it, or the
            # mailbox owner sent it to them. A contact who is merely a fellow
            # recipient of a third party's blast does not count — and a
            # message with no engaged contact at all is not stored.
            matches = {
                contact_map[email]
                for kind, email, _ in participants
                if email != owner_email
                and email in contact_map
                and (kind == "from" or parsed["is_outgoing"])
            }
            if not matches:
                continue
            text = html = None
            if archive:
                found: dict = {}
                _walk_parts(res.get("payload") or {}, found)
                text = (found.get("text") or "")[:MAX_BODY_CHARS] or None
                html = (found.get("html") or "")[:MAX_BODY_CHARS] or None
                pending_chars += len(text or "") + len(html or "")
            # Only the parsed fields are kept — the raw payload is dropped now.
            pending.append((parsed, participants, matches, text, html))
        if rate_limited is not None:
            # Keep what this pass already paid quota for, then propagate so
            # the caller holds its checkpoint and backs off.
            await write_group()
            await db.commit()
            raise rate_limited
        queued = sum(1 for r in failures.values() if not r.given_up) + len(fetch_failed)
        if queued > MAX_PENDING_MESSAGE_FAILURES:
            await write_group()
            await db.commit()
            raise GoogleError(
                "Gmail is failing to return messages — holding the sync position until it recovers"
            )
        if (
            len(pending) + len(fetch_failed) >= WRITE_GROUP_MESSAGES
            or pending_chars >= WRITE_GROUP_BODY_CHARS
        ):
            await write_group()
    await write_group()
    return stored, matched_people


async def _retry_failed_messages(
    db, account: GoogleAccount, access: str, contact_map: dict, attempted: set[str]
) -> int:
    """Start of every pass: another go at the messages earlier passes could
    not fetch. Bounded by the attempt counter on each row."""
    rows = await db.execute(
        select(GmailSyncFailure.key).where(
            GmailSyncFailure.user_id == account.user_id,
            GmailSyncFailure.kind == "message",
            GmailSyncFailure.given_up.is_(False),
        )
    )
    ids = [k for (k,) in rows]
    if not ids:
        return 0
    stored, _ = await _store_messages(db, account, access, ids, contact_map, attempted)
    # Whatever failed again is already counted; don't let a re-listing later
    # in this pass count it twice.
    attempted.update(ids)
    await db.commit()
    return stored


async def _update_person_aggregates(db, org_id: uuid.UUID, person_ids: set[uuid.UUID]) -> None:
    from app.services.interactions import update_person_aggregates

    await update_person_aggregates(db, org_id, person_ids)


def _decode_body(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _walk_parts(payload: dict, found: dict) -> None:
    mime = payload.get("mimeType", "")
    data = (payload.get("body") or {}).get("data")
    if data and mime in ("text/plain", "text/html"):
        key = "text" if mime == "text/plain" else "html"
        found.setdefault(key, _decode_body(data))
    for part in payload.get("parts") or []:
        _walk_parts(part, found)


class MessageGone(GoogleError):
    """Gmail has no message by this id in the connected mailbox."""


async def fetch_message_body(access: str, gmail_id: str) -> dict:
    """Full message content: first text/plain and text/html parts."""
    try:
        item = await _get_json(
            f"{settings.google_gmail_base}/users/me/messages/{gmail_id}",
            access,
            {"format": "full"},
        )
    except GoogleError as exc:
        if exc.status_code == 404:
            # Ids are per mailbox. After the owner connects a different
            # Google account, the rows archived from the old one point at
            # messages the new mailbox never had.
            raise MessageGone(str(exc), status_code=404) from exc
        raise
    found: dict = {}
    _walk_parts(item.get("payload") or {}, found)
    return {"text": found.get("text"), "html": found.get("html")}



ADDRESSES_PER_QUERY = 10
# The combined search is the heaviest query in the system; give it room before
# calling it a timeout.
SEARCH_TIMEOUT = 45.0
# How many message ids one backfill pass lists before it stops and lets the
# next pass carry on. This bounds a PASS, not the history: the walk's position
# (last finished address + the page token inside the current group's search)
# is saved, so a mailbox with tens of thousands of matches is traversed to the
# end over as many passes as it takes. Nothing past the bound is skipped.
BACKFILL_MAX_IDS_PER_PASS = 4000
# Kept under the old name for the diagnostics that still import it.
BACKFILL_MAX_IDS_PER_CHUNK = BACKFILL_MAX_IDS_PER_PASS
BACKFILL_ADDRESSES_PER_CHECKPOINT = ADDRESSES_PER_QUERY


def _is_rate_limit(exc: "GoogleError") -> bool:
    s = str(exc)
    return "(429)" in s or "(403)" in s


def _expand_addresses(addresses: list[str]) -> list[str]:
    expanded: list[str] = []
    seen: set[str] = set()
    for a in addresses:
        for variant in (a.lower().strip(), normalize_email(a)):
            if variant and variant not in seen:
                seen.add(variant)
                expanded.append(variant)
    return expanded


def _contact_query(addrs: list[str]) -> str:
    # Header operators match literal addresses; the quoted free-text term
    # catches variants the operators miss (e.g. dotted gmail headers).
    # Over-fetching is fine: the metadata matcher filters on real headers.
    clause = " OR ".join(
        f'from:{a} OR to:{a} OR cc:{a} OR "{a}"' for a in _expand_addresses(addrs)
    )
    # GMAIL_BACKFILL_DAYS=0 means no window at all: every page of every
    # address's history is walked.
    window = (
        f"newer_than:{settings.gmail_backfill_days}d " if settings.gmail_backfill_days > 0 else ""
    )
    return f"{window}-in:spam -in:trash ({clause})"


async def _search_page(access: str, q: str, page_token: str | None = None) -> dict:
    params = {"q": q, "maxResults": 500}
    if page_token:
        params["pageToken"] = page_token
    return await _get_json(
        f"{settings.google_gmail_base}/users/me/messages", access, params,
        timeout=SEARCH_TIMEOUT,
    )


async def _run_search(access: str, q: str, max_ids: int | None = None) -> list[str]:
    """A whole search held in memory — for the bounded callers (suggestion
    scan, diagnostics). The backfill pages through _search_page itself so it
    can checkpoint between pages."""
    ids: list[str] = []
    seen: set[str] = set()
    page_token = None
    while True:
        data = await _search_page(access, q, page_token)
        for m in data.get("messages", []):
            mid = m.get("id")
            if mid and mid not in seen:
                seen.add(mid)
                ids.append(mid)
        if max_ids is not None and len(ids) >= max_ids:
            break  # newest-first; stop once we have enough recent history
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return ids


async def _search_contact_mail(
    access: str, addresses: list[str], max_ids: int = BACKFILL_MAX_IDS_PER_PASS
) -> list[str]:
    """A capped sample of the mail exchanged with the given addresses — the
    diagnostics endpoints' view of what the backfill would search. NOT the
    backfill itself (see _walk_group): this stops at max_ids."""
    ids: list[str] = []
    seen: set[str] = set()
    for i in range(0, len(addresses), ADDRESSES_PER_QUERY):
        remaining = max_ids - len(ids)
        if remaining <= 0:
            break
        group = addresses[i : i + ADDRESSES_PER_QUERY]
        for mid in await _run_search(access, _contact_query(group), max_ids=remaining):
            if mid not in seen:
                seen.add(mid)
                ids.append(mid)
    return ids


def _backfill_addresses(contact_map: dict) -> list[str]:
    """People's addresses drive the backfill (leads only if opted in), sorted
    so the walk's position is stable across restarts."""
    return sorted(
        a
        for a, (etype, _) in contact_map.items()
        if etype == "person" or settings.gmail_backfill_leads
    )


def count_backfill_addresses(addr_map: dict) -> int:
    """How long the walk over addr_map would be — the same rule as
    _backfill_addresses, for any map keyed by normalized address whose values
    lead with the entity type (the feed's cached contact map qualifies)."""
    return sum(
        1 for hit in addr_map.values() if hit[0] == "person" or settings.gmail_backfill_leads
    )


def backfill_position(addresses: list[str], after: str | None) -> int:
    """Index of the first address the walk still owes. The walk remembers the
    last address it finished rather than an index, so contacts added or
    removed mid-walk can't shift it past someone unsearched."""
    return bisect.bisect_right(addresses, after) if after else 0


def _query_fingerprint(q: str) -> str:
    return hashlib.sha256(q.encode()).hexdigest()[:16]


async def _walk_group(
    db, account: GoogleAccount, access: str, group: list[str], contact_map: dict,
    budget: int, attempted: set[str],
) -> tuple[bool, int, int]:
    """Search one group of addresses and store what matches, a page at a
    time. Returns (finished, ids_listed, stored).

    After each page is stored, the next page's token is committed (tagged
    with the query it belongs to), so a pass that runs out of budget — or a
    restart — resumes at that page. finished=True only when Gmail reports no
    further page: that is the only thing that lets the caller advance."""
    q = _contact_query(group)
    tag = _query_fingerprint(q)
    token = None
    saved = account.gmail_backfill_page_token or ""
    if saved.startswith(f"{tag}:"):
        token = saved[len(tag) + 1 :]
    listed = stored = 0
    while True:
        await _release_db(db)
        try:
            data = await _search_page(access, q, token)
        except GoogleError as exc:
            if token and "(400)" in str(exc):
                # Gmail no longer honours the saved token — start this
                # group's search over; the dedupe absorbs the repeats.
                log.info("gmail backfill page token rejected; restarting the group's search")
                token = None
                account.gmail_backfill_page_token = None
                continue
            raise
        ids = [m["id"] for m in data.get("messages", []) if m.get("id")]
        got, _ = await _store_messages(db, account, access, ids, contact_map, attempted)
        stored += got
        listed += len(ids)
        token = data.get("nextPageToken")
        if not token:
            return True, listed, stored
        position = f"{tag}:{token}"
        resumable = len(position) <= 1000
        if resumable:
            account.gmail_backfill_page_token = position
        await db.commit()  # checkpoint inside the group
        if listed >= budget and resumable:
            return False, listed, stored


async def _reset_given_up(db, user_id: uuid.UUID) -> None:
    await db.execute(
        update(GmailSyncFailure)
        .where(GmailSyncFailure.user_id == user_id, GmailSyncFailure.given_up.is_(True))
        .values(given_up=False, attempts=0)
    )


async def sync_gmail(
    db, cfg: GoogleIntegration, account: GoogleAccount, force_backfill: bool = False,
    progress: dict | None = None,
) -> int:
    """Targeted backfill (searches all history with known contacts, or the
    configured window), then the history feed for new mail. Only mail involving known People/Leads is
    stored. force_backfill re-runs the search — how newly added contacts get
    their history pulled.

    The backfill runs in bounded passes and is only ever marked done when the
    walk has gone past the last address with every search completed. When a
    pass stops only because its budget ran out, progress["backfill_paused"]
    is set so the loop knows to come back soon."""
    access = await ensure_access_token(db, cfg, account)
    contact_map = await _crm_email_map(db, account.org_id)
    if not contact_map:
        return 0
    stored_total = 0
    attempted: set[str] = set()

    if force_backfill:
        # An explicit re-run is also a fresh chance for anything given up on.
        await _reset_given_up(db, account.user_id)
        if account.gmail_backfill_done:
            # Restart the walk — how newly added contacts get their history.
            account.gmail_backfill_done = False
            account.gmail_backfill_cursor = 0
            account.gmail_backfill_after = None
            account.gmail_backfill_page_token = None

    # Messages earlier passes couldn't fetch come first, so a transient
    # failure heals on the next pass no matter where the checkpoints are.
    stored_total += await _retry_failed_messages(db, account, access, contact_map, attempted)

    if not account.gmail_backfill_done:
        # Snapshot the history cursor FIRST so mail arriving mid-backfill
        # isn't missed once the incremental feed takes over.
        if not account.gmail_history_id:
            await _release_db(db)
            profile = await _get_json(f"{settings.google_gmail_base}/users/me/profile", access)
            account.gmail_history_id = str(profile.get("historyId") or "")
            await db.commit()
        addresses = _backfill_addresses(contact_map)
        # Stamped per pass (the list is rebuilt each time anyway) so the
        # status poll can report progress without sizing the walk itself.
        account.gmail_backfill_total = len(addresses)
        pos = backfill_position(addresses, account.gmail_backfill_after)
        account.gmail_backfill_cursor = pos
        address_failures = await _failure_rows(db, account.user_id, "address")
        budget = BACKFILL_MAX_IDS_PER_PASS
        single_until = 0  # addresses below this index are searched one by one
        while pos < len(addresses):
            if pos < single_until:
                group = [addresses[pos]]
            else:
                group = addresses[pos : pos + ADDRESSES_PER_QUERY]
            try:
                finished, listed, stored = await _walk_group(
                    db, account, access, group, contact_map, budget, attempted
                )
            except GoogleError as exc:
                if _is_rate_limit(exc):
                    # Quota pressure: keep the checkpoint, resume next pass.
                    account.sync_error = "Gmail rate limited — backfill resumes automatically"
                    await db.commit()
                    log.warning("gmail backfill rate limited at %s/%s", pos, len(addresses))
                    return stored_total
                if len(group) > 1:
                    # A combined query that fails any other way — usually a
                    # timeout on a large mailbox — degrades to one address at
                    # a time so a single heavy contact can't jam the rest.
                    log.warning("combined gmail search failed (%s); retrying per address", exc)
                    single_until = pos + len(group)
                    account.gmail_backfill_page_token = None
                    continue
                # One address's search failed. The walk does not move past
                # it: the failure is counted and the pass ends here, to try
                # again next time. Only after MAX_FAILURE_ATTEMPTS is the
                # address recorded as given up and stepped over.
                given_up = _note_failure(
                    db, address_failures, account.user_id, "address", group[0], exc
                )
                if not given_up:
                    account.sync_error = (
                        f"Gmail search failed for {group[0]} — the backfill will retry it"
                    )[:500]
                    await db.commit()
                    return stored_total
                finished, listed, stored = True, 0, 0
            else:
                if finished:
                    await _clear_failures(db, address_failures, group)
            stored_total += stored
            budget -= listed
            if finished:
                pos += len(group)
                account.gmail_backfill_after = group[-1]
                account.gmail_backfill_cursor = pos
                account.gmail_backfill_page_token = None
            account.sync_error = None
            await db.commit()  # checkpoint: finished work survives anything
            if not finished or (budget <= 0 and pos < len(addresses)):
                # Pass budget spent with history still to walk; the saved
                # position picks it up on the next pass.
                log.info("gmail backfill paused at %s/%s for %s", pos, len(addresses), account.user_id)
                if progress is not None:
                    progress["backfill_paused"] = True
                return stored_total
        account.gmail_backfill_done = True
        account.gmail_backfill_cursor = 0
        account.gmail_backfill_total = None
        account.gmail_backfill_after = None
        account.gmail_backfill_page_token = None
    elif account.gmail_history_id:
        ids = []
        page_token = None
        newest_history = account.gmail_history_id
        await _release_db(db)
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
                account.gmail_backfill_cursor = 0
                account.gmail_backfill_after = None
                account.gmail_backfill_page_token = None
                return stored_total
            raise
        # A message that won't fetch is queued for retry inside, which is
        # what makes it safe to move the history id past it below.
        stored, _ = await _store_messages(db, account, access, ids, contact_map, attempted)
        stored_total += stored
        # Advanced only after every batch is in: an interruption re-walks
        # this history window next pass and the dedupe absorbs the repeats.
        account.gmail_history_id = newest_history

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
    progress: dict = {}
    emails = (
        await sync_gmail(db, cfg, account, force_backfill=force_backfill, progress=progress)
        if has_gmail_scope(account)
        else 0
    )
    return {
        "events_synced": events,
        "emails_synced": emails,
        "backfill_paused": bool(progress.get("backfill_paused")),
    }


def _sync_error_text(exc: Exception) -> str:
    # GoogleError messages are already curated; anything else gets its type
    # named so the status is never a mystery.
    if isinstance(exc, GoogleError):
        return str(exc)[:500]
    return f"Sync failed: {type(exc).__name__}: {exc}"[:500]


async def _record_sync_error(user_id: uuid.UUID, exc: Exception) -> None:
    # Its own session: the one the sync used may be mid-transaction or hold
    # objects a rollback expired, and nothing here should depend on either.
    async with SessionLocal() as db:
        account = (
            await db.execute(select(GoogleAccount).where(GoogleAccount.user_id == user_id))
        ).scalar_one_or_none()
        if account is not None:
            account.sync_error = _sync_error_text(exc)
            await db.commit()


# ---- one sync at a time per account ----
#
# A manual sync and the scheduled pass would otherwise race on the same
# checkpoints and unique message rows. The app is a single process, so an
# in-process claim is enough; it is taken synchronously (no await between the
# check and the claim), which is what makes it race-free on one event loop.

_syncing: set[uuid.UUID] = set()


def try_claim_sync(user_id: uuid.UUID) -> bool:
    if user_id in _syncing:
        return False
    _syncing.add(user_id)
    return True


def release_sync(user_id: uuid.UUID) -> None:
    _syncing.discard(user_id)


def sync_in_progress(user_id: uuid.UUID) -> bool:
    return user_id in _syncing


async def _sync_claimed_account(user_id: uuid.UUID, label: str, force_backfill: bool) -> bool:
    """Run one account's sync in a session of its own. The caller holds the
    claim. Returns True when the backfill stopped only because the pass
    budget ran out (so the loop should come back soon) — not when it is rate
    limited, holding on a failed search, or has nothing to walk."""
    try:
        async with SessionLocal() as db:
            account = (
                await db.execute(select(GoogleAccount).where(GoogleAccount.user_id == user_id))
            ).scalar_one_or_none()
            if account is None:
                return False
            result = await sync_account(db, account, force_backfill=force_backfill)
            await db.commit()
            return result["backfill_paused"]
    except Exception as exc:
        # Record ANY failure, not just GoogleError — an unrecorded crash
        # leaves a stale status that looks like nothing changed. `label` is a
        # plain string captured up front: after a failed transaction the ORM
        # object's attributes are expired and must not be touched.
        log.warning("Google sync failed for %s: %s", label, exc)
        try:
            await _record_sync_error(user_id, exc)
        except Exception:
            log.exception("could not record the Google sync error for %s", label)
        return False


async def run_sync_pass() -> bool:
    """Sync every connected mailbox whose CRM user is still active. Accounts
    are read as plain tuples and each one syncs in a fresh session, so one
    account's failure (and the rollback that follows) can't poison the next.
    Returns True when some mailbox still has backfill to continue."""
    async with SessionLocal() as db:
        accounts = (
            await db.execute(
                select(GoogleAccount.user_id, GoogleAccount.email)
                .join(User, User.id == GoogleAccount.user_id)
                # A deactivated user's mailbox stops syncing the moment they
                # are switched off; the stored grant is left for an admin to
                # unlink or for reactivation to resume.
                .where(User.is_active.is_(True))
                .order_by(GoogleAccount.connected_at)
            )
        ).all()
    more = False
    for user_id, email in accounts:
        if not try_claim_sync(user_id):
            log.info("Google sync for %s already running; skipping this pass", email)
            continue
        try:
            more = await _sync_claimed_account(user_id, email, False) or more
        finally:
            release_sync(user_id)
    return more


# How many recent messages to scan per mailbox folder when suggesting contacts.
SUGGESTION_SCAN_LIMIT = 400
# Local-part tokens that are obviously automated — never worth suggesting as
# a contact. Matched against the LOCAL PART only, so a real address like
# greenews@example.com isn't caught by "news".
SUGGESTION_LOCAL_EXACT = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "no_reply",
    "mailer-daemon", "postmaster", "bounce", "notifications", "notification",
    "alerts", "updates", "news", "newsletter", "support", "info", "hello",
    "automated", "marketing", "billing", "sales",
}
SUGGESTION_LOCAL_PREFIXES = ("noreply", "no-reply", "donotreply", "do-not-reply", "bounce")


def _looks_automated(email: str) -> bool:
    local = email.lower().split("@", 1)[0]
    return local in SUGGESTION_LOCAL_EXACT or local.startswith(SUGGESTION_LOCAL_PREFIXES)


async def scan_contact_suggestions(user_id: uuid.UUID) -> None:
    """Scan recent sent/received mail for frequent correspondents who aren't
    CRM contacts yet, and upsert them as suggestions. On-demand and bounded."""
    async with SessionLocal() as db:
        account = (
            await db.execute(select(GoogleAccount).where(GoogleAccount.user_id == user_id))
        ).scalar_one_or_none()
        if account is None or not has_gmail_scope(account):
            return
        cfg = (
            await db.execute(
                select(GoogleIntegration).where(GoogleIntegration.org_id == account.org_id)
            )
        ).scalar_one_or_none()
        if cfg is None:
            return
        try:
            access = await ensure_access_token(db, cfg, account)
            owner_email = normalize_email(account.email)
            # Fetch first, write after: the scan is up to two searches and
            # several hundred message reads, none of which may sit inside a
            # transaction. This also persists a refreshed token.
            await _release_db(db)

            tally: dict[str, dict] = {}
            for query in ("in:sent", "in:inbox"):
                ids = await _run_search(
                    access, f"{query} -in:spam -in:trash", max_ids=SUGGESTION_SCAN_LIMIT
                )
                for i in range(0, len(ids), 10):
                    chunk = ids[i : i + 10]
                    items = await asyncio.gather(
                        *[_fetch_message(access, g, False) for g in chunk],
                        return_exceptions=True,
                    )
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        parsed = _parse_message(item, owner_email)
                        if parsed is None:
                            continue
                        sent_at = parsed["sent_at"]
                        outgoing = bool(parsed["is_outgoing"])
                        for kind, email, name in parsed["participants"]:
                            if not email or email == owner_email or "@" not in email:
                                continue
                            if _looks_automated(email):
                                continue
                            # "You emailed them" = you sent to this recipient.
                            # "They emailed you" = they are the sender inbound.
                            counts = outgoing and kind in ("to", "cc")
                            counts = counts or (not outgoing and kind == "from")
                            if not counts:
                                continue
                            t = tally.setdefault(
                                email, {"count": 0, "out": 0, "name": None, "last": None}
                            )
                            t["count"] += 1
                            if outgoing:
                                t["out"] += 1
                            if name and not t["name"]:
                                t["name"] = name
                            if sent_at and (t["last"] is None or sent_at > t["last"]):
                                t["last"] = sent_at

            # The network part is over; everything from here is local. Read
            # the CRM's addresses and the standing suggestions now, so they
            # are as fresh as the write that follows.
            contact_map = await _crm_email_map(db, account.org_id)  # already-known addresses
            existing = {
                s.email: s
                for s in (
                    await db.execute(
                        select(ContactSuggestion).where(
                            ContactSuggestion.org_id == account.org_id
                        )
                    )
                ).scalars()
            }
            for email, t in tally.items():
                if email in contact_map:  # already a Person/Lead
                    continue
                # A single message you never replied to isn't worth suggesting.
                # Qualify only if you emailed them at least once, or there's a
                # real back-and-forth (2+ messages).
                if t["out"] == 0 and t["count"] < 2:
                    continue
                s = existing.get(email)
                if s is None:
                    db.add(
                        ContactSuggestion(
                            org_id=account.org_id, email=email, display_name=t["name"],
                            message_count=t["count"], last_seen_at=t["last"], status="pending",
                        )
                    )
                else:
                    # Keep ignored/added decisions; just refresh the stats.
                    s.message_count = t["count"]
                    if t["name"] and not s.display_name:
                        s.display_name = t["name"]
                    if t["last"] and (s.last_seen_at is None or t["last"] > s.last_seen_at):
                        s.last_seen_at = t["last"]
            await db.commit()
            log.info("contact-suggestion scan for %s: %s candidates", account.email, len(tally))
        except GoogleError as exc:
            log.warning("suggestion scan failed for %s: %s", account.email, exc)


async def recompute_all_person_metrics(org_id: uuid.UUID) -> None:
    """Rebuild interaction counts / last-contacted for every person in the
    org from stored emails and phone events. Repair tool for data synced
    before metrics were tracked per checkpoint."""
    from app.models import Person
    from app.services.interactions import update_person_aggregates

    async with SessionLocal() as db:
        ids = [
            pid
            for (pid,) in await db.execute(
                select(Person.id).where(
                    Person.org_id == org_id, Person.deleted_at.is_(None)
                )
            )
        ]
        for i in range(0, len(ids), 200):
            await update_person_aggregates(db, org_id, set(ids[i : i + 200]))
            await db.commit()
        log.info("recomputed interaction metrics for %s people", len(ids))


async def sync_account_background(
    user_id: uuid.UUID, force_backfill: bool = False, claimed: bool = False,
    label: str | None = None,
) -> None:
    """A user-triggered sync, detached from its request — a full backfill can
    run for many minutes, far past any request timeout. `claimed` means the
    caller already took the per-account claim (the endpoint does, so it can
    answer "already running" on the spot); it is released here either way."""
    if not claimed and not try_claim_sync(user_id):
        log.info("Google sync for %s already running; not starting another", user_id)
        return
    try:
        # A pass is bounded; someone who pressed Sync wants the whole walk, so
        # keep taking passes while the only thing stopping it is the budget.
        # Only the first one may be forced — forcing again would restart the
        # walk it just began.
        for _ in range(MANUAL_SYNC_MAX_PASSES):
            paused = await _sync_claimed_account(user_id, label or str(user_id), force_backfill)
            if not paused:
                break
            force_backfill = False
    finally:
        release_sync(user_id)


async def sync_loop() -> None:
    while True:
        more = False
        try:
            more = await run_sync_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Google sync pass failed")
        await asyncio.sleep(BACKFILL_CONTINUE_SECONDS if more else SYNC_INTERVAL_SECONDS)
