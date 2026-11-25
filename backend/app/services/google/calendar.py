"""Calendar sync: store upcoming and recent events with their attendees."""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.config import settings
from app.models import (
    CalendarEvent,
    CalendarEventAttendee,
    GoogleAccount,
    GoogleIntegration,
    utcnow,
)
from app.services.contacts import normalize_email
from app.services.google.client import _get_json, release_db, ensure_access_token

log = logging.getLogger("google")


EVENT_WINDOW_PAST_DAYS = 30
EVENT_WINDOW_FUTURE_DAYS = 90


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
    # Attendee rows go with their event (ON DELETE CASCADE).
    for i in range(0, len(event_ids), 5000):
        chunk = event_ids[i : i + 5000]
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
        await release_db(db)
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
