"""The timeline: emails, calls, texts, notes and activity as one time-ordered
stream — for the whole org (the feed) or for one record (its detail page).

Each kind of item is a SOURCE: a skinny (kind, id, ts) select that says which
rows belong and when they happened, and a hydrator that turns a page of ids
into the items clients render. Ordering and paging happen in the database
over the union of the sources; only the rows on the requested page are ever
read in full.

The shapes are defined here and nowhere else: email_row, phone_row, note_row
and activity_row are what the list endpoints (`/emails`,
`/integrations/ringcentral/events`, `/notes`, `/activities`) return, and a
timeline item is built from that same row — `type`, `at` and `related` in
place of the row's own kind and timestamp (see _item).

Adding a source means adding one entry to SOURCES.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import exists, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity, EmailMessage, EmailParticipant, Note, PhoneEvent
from app.services import entities
from app.services.common import display_name_map, entity_labels_map, row_to_dict
from app.services.contacts import ContactPoints, cached_contact_index, contact_points

# Deep pages are OFFSET scans — cheap skinny rows, but still work that grows
# with the page number, and no reader scrolls that far: the iOS app stops at
# 10 pages and points at the web app for older history. Past the cap a
# timeline just ends.
MAX_PAGE = 200


# ---- scope: one record's slice of the timeline ----


@dataclass
class RecordScope:
    entity_type: str
    entity_id: uuid.UUID
    # What the record answers for; None for a record type with no
    # correspondence (an opportunity has notes and activity only).
    points: ContactPoints | None


async def record_scope(
    db: AsyncSession, org_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID
) -> RecordScope | None:
    """The scope for one in-org record, or None when it doesn't exist.
    Raises KeyError for an unknown record type."""
    obj = await entities.get_record(db, org_id, entity_type, entity_id)
    if obj is None:
        return None
    points = None
    if entities.RECORDS[entity_type].correspondent:
        points = await contact_points(db, org_id, entity_type, obj)
    return RecordScope(entity_type, entity_id, points)


async def require_scope(
    db: AsyncSession,
    org_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    *,
    correspondents_only: bool = False,
) -> RecordScope:
    """record_scope for a route: 422 for a type that has no such timeline,
    404 for a record that isn't there."""
    rec = entities.RECORDS.get(entity_type)
    if correspondents_only and (rec is None or not rec.correspondent):
        raise HTTPException(status_code=422, detail="entity_type must be person, lead or company")
    if rec is None:
        raise HTTPException(status_code=422, detail=f"Unknown entity type: {entity_type}")
    scope = await record_scope(db, org_id, entity_type, entity_id)
    if scope is None:
        raise HTTPException(status_code=404, detail=f"{entity_type} not found")
    return scope


def email_belongs(scope: RecordScope):
    """SQL condition on EmailParticipant: this participant's address is one
    the record answers for. A company's correspondence is anyone at its email
    domain plus every address on its people (which catches a contact's
    personal Gmail, missed by the domain alone)."""
    points = scope.points
    conds = []
    if points is not None and points.domain:
        conds.append(EmailParticipant.email.like(f"%@{points.domain}"))
    if points is not None and points.emails:
        conds.append(EmailParticipant.email.in_(points.emails))
    return or_(*conds) if conds else None


def phone_belongs(scope: RecordScope):
    """SQL condition on PhoneEvent: the call/text sits on this record's
    timeline — logged directly on it, or matched to one of its numbers. For a
    company that is everyone associated with it: any of its people's numbers,
    and events logged on those people."""
    if scope.points is None:
        return None
    conds = [
        (PhoneEvent.entity_type == scope.entity_type) & (PhoneEvent.entity_id == scope.entity_id)
    ]
    if scope.points.numbers:
        conds.append(PhoneEvent.other_number.in_(scope.points.numbers))
    if scope.points.person_ids:
        conds.append(
            (PhoneEvent.entity_type == "person")
            & (PhoneEvent.entity_id.in_(scope.points.person_ids))
        )
    return or_(*conds)


def phone_event_on_record(event: PhoneEvent, scope: RecordScope) -> bool:
    """phone_belongs, for one event already in hand."""
    if scope.points is None:
        return False
    if event.entity_type == scope.entity_type and event.entity_id == scope.entity_id:
        return True
    if event.entity_type == "person" and event.entity_id in scope.points.person_ids:
        return True
    return bool(event.other_number) and event.other_number in scope.points.numbers


# ---- row shapes ----

EMAIL_COLUMNS = (
    EmailMessage.id,
    EmailMessage.subject,
    EmailMessage.snippet,
    EmailMessage.from_email,
    EmailMessage.from_name,
    EmailMessage.is_outgoing,
    EmailMessage.sent_at,
    EmailMessage.created_at,
    EmailMessage.gmail_id,
    EmailMessage.owner_user_id,
)
"""Skinny columns only — archived bodies can be a megabyte each, and no list
ever returns them."""


def email_row(m) -> dict:
    return {
        "id": str(m.id),
        "subject": m.subject,
        "snippet": m.snippet,
        "from_email": m.from_email,
        "from_name": m.from_name,
        "is_outgoing": m.is_outgoing,
        "sent_at": m.sent_at.isoformat() if m.sent_at else None,
        "gmail_id": m.gmail_id,
        "owner_user_id": str(m.owner_user_id) if m.owner_user_id else None,
    }


def phone_row(e: PhoneEvent) -> dict:
    return {
        "id": str(e.id),
        "kind": e.kind,  # call | sms
        "direction": e.direction,
        "other_number": e.other_number,
        "other_name": e.other_name,
        "happened_at": e.happened_at.isoformat() if e.happened_at else None,
        "duration_seconds": e.duration_seconds,
        "result": e.result,
        "text": e.text,
        "recording_id": e.recording_id,
    }


def note_row(n: Note, author_names: dict[str, str]) -> dict:
    d = row_to_dict(n)
    d["author_name"] = author_names.get(d.get("author_id"))
    return d


def activity_row(a: Activity, actor_names: dict[str, str]) -> dict:
    d = row_to_dict(a)
    d["actor_name"] = actor_names.get(d.get("actor_id"))
    return d


async def note_rows(db: AsyncSession, org_id: uuid.UUID, notes: list[Note]) -> list[dict]:
    names = await display_name_map(db, {n.author_id for n in notes}, org_id)
    return [note_row(n, names) for n in notes]


async def activity_rows(db: AsyncSession, org_id: uuid.UUID, acts: list[Activity]) -> list[dict]:
    names = await display_name_map(db, {a.actor_id for a in acts}, org_id)
    return [activity_row(a, names) for a in acts]


def _ref(entity_type: str, entity_id, label: str) -> dict:
    return {"entity_type": entity_type, "entity_id": str(entity_id), "label": label}


async def email_related(
    db: AsyncSession, org_id: uuid.UUID, email_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[dict]]:
    """Who in the CRM each email touches, via its engaged participants."""
    out: dict[uuid.UUID, list[dict]] = {}
    if not email_ids:
        return out
    parts = await db.execute(
        select(EmailParticipant.email_id, EmailParticipant.email).where(
            EmailParticipant.email_id.in_(email_ids),
            EmailParticipant.direct.is_(True),
        )
    )
    by_addr: dict[str, list[uuid.UUID]] = {}
    for eid, addr in parts:
        by_addr.setdefault(addr, []).append(eid)
    if not by_addr:
        return out
    owners = (await cached_contact_index(db, org_id)).by_email
    for addr, eids in by_addr.items():
        hit = owners.get(addr)
        if not hit:
            continue
        entry = _ref(*hit)
        for eid in eids:
            bucket = out.setdefault(eid, [])
            if entry not in bucket:
                bucket.append(entry)
    return out


def _item(type_: str, at: str, row: dict, *, without: tuple[str, ...], **extra) -> dict:
    """A timeline item from a list row. `type` and `at` say what the row's
    own kind/timestamp keys said, so those are left out; the feed's item
    shape is a published contract and stays exactly as it was."""
    item = {"type": type_, "at": at}
    item.update((k, v) for k, v in row.items() if k not in without)
    item.update(extra)
    return item


# ---- sources ----

_EMAIL_TS = func.coalesce(EmailMessage.sent_at, EmailMessage.created_at)
_PHONE_TS = func.coalesce(PhoneEvent.happened_at, PhoneEvent.created_at)


def _skinny(kind: str, id_col, ts):
    return select(literal(kind).label("kind"), id_col.label("id"), ts.label("ts"))


def _email_stream(org_id, scope: RecordScope | None):
    engaged = (EmailParticipant.email_id == EmailMessage.id) & EmailParticipant.direct.is_(True)
    if scope is not None:
        belongs = email_belongs(scope)
        if belongs is None:
            return None
        engaged = engaged & belongs
    return _skinny("email", EmailMessage.id, _EMAIL_TS).where(
        EmailMessage.org_id == org_id, exists().where(engaged)
    )


def _phone_stream(org_id, scope: RecordScope | None):
    stmt = _skinny("phone", PhoneEvent.id, _PHONE_TS).where(PhoneEvent.org_id == org_id)
    if scope is not None:
        belongs = phone_belongs(scope)
        if belongs is None:
            return None
        stmt = stmt.where(belongs)
    return stmt


def _note_stream(org_id, scope: RecordScope | None):
    stmt = _skinny("note", Note.id, Note.created_at).where(Note.org_id == org_id)
    if scope is not None:
        stmt = stmt.where(
            Note.entity_type == scope.entity_type, Note.entity_id == scope.entity_id
        )
        # A note linked to a call/text that is on this timeline is shown
        # under that event (see _hydrate_phone), not as an item of its own.
        belongs = phone_belongs(scope)
        if belongs is not None:
            shown = select(PhoneEvent.id).where(PhoneEvent.org_id == org_id, belongs)
            stmt = stmt.where(Note.phone_event_id.is_(None) | Note.phone_event_id.not_in(shown))
    return stmt


def _activity_stream(org_id, scope: RecordScope | None):
    stmt = _skinny("activity", Activity.id, Activity.created_at).where(Activity.org_id == org_id)
    if scope is not None:
        stmt = stmt.where(
            Activity.entity_type == scope.entity_type, Activity.entity_id == scope.entity_id
        )
    return stmt


async def _hydrate_emails(db, org_id, ids: list, scope: RecordScope | None) -> dict:
    emails = (
        await db.execute(
            select(*EMAIL_COLUMNS).where(EmailMessage.org_id == org_id, EmailMessage.id.in_(ids))
        )
    ).all()
    # On a record's own timeline every item is about that record; the chips
    # that say so are only worth computing for the org-wide feed.
    related = await email_related(db, org_id, ids) if scope is None else {}
    return {
        e.id: _item(
            "email",
            (e.sent_at or e.created_at).isoformat(),
            email_row(e),
            without=("sent_at",),
            related=related.get(e.id, []),
        )
        for e in emails
    }


async def _hydrate_phone(db, org_id, ids: list, scope: RecordScope | None) -> dict:
    events = (
        (
            await db.execute(
                select(PhoneEvent).where(PhoneEvent.org_id == org_id, PhoneEvent.id.in_(ids))
            )
        )
        .scalars()
        .all()
    )
    owners: dict = {}
    notes_by_event: dict[uuid.UUID, list[dict]] = {}
    if events and scope is None:
        owners = (await cached_contact_index(db, org_id)).by_number
    elif events:
        # This record's notes about each call/text ride along with it.
        notes = (
            (
                await db.execute(
                    select(Note)
                    .where(
                        Note.org_id == org_id,
                        Note.entity_type == scope.entity_type,
                        Note.entity_id == scope.entity_id,
                        Note.phone_event_id.in_([e.id for e in events]),
                    )
                    .order_by(Note.created_at.desc(), Note.id)
                )
            )
            .scalars()
            .all()
        )
        for row, n in zip(await note_rows(db, org_id, list(notes)), notes):
            notes_by_event.setdefault(n.phone_event_id, []).append(row)
    out = {}
    for e in events:
        # A manually logged event carries its own record link; its label is
        # resolved with the other entity labels. Synced ones match by number.
        hit = None if (e.entity_type and e.entity_id) else owners.get(e.other_number)
        item = _item(
            e.kind,
            (e.happened_at or e.created_at).isoformat(),
            phone_row(e),
            without=("kind", "happened_at"),
            entity_type=e.entity_type,
            entity_id=str(e.entity_id) if e.entity_id else None,
            related=[_ref(*hit)] if hit else [],
        )
        if scope is not None:
            item["notes"] = notes_by_event.get(e.id, [])
        out[e.id] = item
    return out


async def _hydrate_notes(db, org_id, ids: list, scope: RecordScope | None) -> dict:
    notes = (
        (await db.execute(select(Note).where(Note.org_id == org_id, Note.id.in_(ids))))
        .scalars()
        .all()
    )
    rows = await note_rows(db, org_id, list(notes))
    # The record page decides who may delete a note and says when one is tied
    # to a call that isn't shown; the feed needs neither.
    without = ("created_at",) if scope is not None else ("created_at", "author_id", "phone_event_id")
    return {
        n.id: _item("note", row["created_at"], row, without=without)
        for n, row in zip(notes, rows)
    }


async def _hydrate_activities(db, org_id, ids: list, scope: RecordScope | None) -> dict:
    acts = (
        (await db.execute(select(Activity).where(Activity.org_id == org_id, Activity.id.in_(ids))))
        .scalars()
        .all()
    )
    rows = await activity_rows(db, org_id, list(acts))
    return {
        a.id: _item("activity", row["created_at"], row, without=("created_at", "actor_id"))
        for a, row in zip(acts, rows)
    }


@dataclass(frozen=True)
class Source:
    # Returns the skinny select, or None when the source has nothing to say
    # in this scope (email on an opportunity).
    stream: Callable
    hydrate: Callable[..., Awaitable[dict]]


SOURCES: dict[str, Source] = {
    "email": Source(_email_stream, _hydrate_emails),
    "phone": Source(_phone_stream, _hydrate_phone),
    "note": Source(_note_stream, _hydrate_notes),
    "activity": Source(_activity_stream, _hydrate_activities),
}
KINDS = ("all", *SOURCES)


def _ordered_ids(org_id: uuid.UUID, kind: str, scope: RecordScope | None, offset: int, limit: int):
    """One statement for the whole timeline's ORDER: a UNION ALL of the
    sources `kind` selects, sorted and paged by the database.

    The order is total — (ts DESC, kind, id) — so items sharing a timestamp
    (a bulk import stamps hundreds) can't shuffle between two page requests.

    Each source is pre-trimmed to its own newest offset+limit rows: no row
    beyond that can reach this page, and it lets Postgres walk the
    (org_id, created_at) indexes instead of sorting whole tables."""
    reach = offset + limit
    parts = []
    for name, source in SOURCES.items():
        if kind not in ("all", name):
            continue
        stmt = source.stream(org_id, scope)
        if stmt is None:
            continue
        ts = stmt.selected_columns.ts
        trimmed = stmt.order_by(ts.desc(), stmt.selected_columns.id).limit(reach).subquery()
        parts.append(select(trimmed.c.kind, trimmed.c.id, trimmed.c.ts))
    if not parts:
        return None
    merged = union_all(*parts).subquery()
    return (
        select(merged.c.kind, merged.c.id, merged.c.ts)
        .order_by(merged.c.ts.desc(), merged.c.kind, merged.c.id)
        .offset(offset)
        .limit(limit)
    )


async def _label_related(db: AsyncSession, org_id: uuid.UUID, items: list[dict]) -> None:
    """Fill `related` for items that point at a record by (type, id): notes,
    activity, and manually logged calls/texts."""
    refs = {
        (i.get("entity_type"), i.get("entity_id"))
        for i in items
        if i["type"] in ("note", "activity", "call", "sms")
    }
    labels = await entity_labels_map(db, org_id, refs, unnamed="(unnamed)")
    for i in items:
        if not (i.get("entity_type") and i.get("entity_id")):
            continue
        label = labels.get((i["entity_type"], i["entity_id"]))
        entry = [_ref(i["entity_type"], i["entity_id"], label)] if label else []
        if i["type"] in ("call", "sms") and not i["related"] and entry:
            i["related"] = entry
        elif i["type"] in ("note", "activity"):
            i["related"] = entry


async def timeline_page(
    db: AsyncSession,
    org_id: uuid.UUID,
    *,
    kind: str = "all",
    page: int = 1,
    page_size: int = 30,
    scope: RecordScope | None = None,
) -> dict:
    """One page of the org's feed (scope=None) or of one record's timeline.
    An unknown `kind` selects nothing, and a page past the cap is an empty
    last page rather than an error: the clients page by number and would
    surface a 4xx as a failure."""
    page = max(1, page)
    page_size = min(max(page_size, 1), 100)
    envelope = {"items": [], "page": page, "page_size": page_size, "has_more": False}
    if page > MAX_PAGE:
        return envelope

    # One extra row tells us whether another page exists.
    stmt = _ordered_ids(org_id, kind, scope, (page - 1) * page_size, page_size + 1)
    order = (await db.execute(stmt)).all() if stmt is not None else []
    envelope["has_more"] = len(order) > page_size
    order = order[:page_size]

    # Hydrate just this page: one bounded lookup per source that appears on
    # it. The work is the same on page 1 and page 150.
    ids_by_source: dict[str, list] = {}
    for source, rid, _ts in order:
        ids_by_source.setdefault(source, []).append(rid)
    hydrated = {
        source: await SOURCES[source].hydrate(db, org_id, ids, scope)
        for source, ids in ids_by_source.items()
    }
    items = [hydrated[source][rid] for source, rid, _ts in order if rid in hydrated[source]]
    for item in items:
        item.setdefault("related", [])
    if scope is None:
        await _label_related(db, org_id, items)
    envelope["items"] = items
    return envelope
