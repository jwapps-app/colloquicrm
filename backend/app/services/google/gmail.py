"""Gmail messages: parse, fetch, store, and the retry queue for work that failed."""

import asyncio
import base64
import logging
import uuid
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr

from sqlalchemy import delete, func, select

from app.config import settings
from app.models import (
    EmailMessage,
    EmailParticipant,
    GmailSyncFailure,
    GoogleAccount,
    utcnow,
)
from app.services.contacts import ContactRef, normalize_email
from app.services.google.client import (
    SEARCH_TIMEOUT,
    GoogleError,
    _get_json,
    _is_rate_limit,
    release_db,
)

log = logging.getLogger("google")


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
    contact_map: dict[str, ContactRef],
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
            group_people.update(ref.entity_id for ref in matches if ref.entity_type == "person")
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
        await release_db(db)
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
