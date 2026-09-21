import time
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import exists, func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import (
    Activity,
    EmailMessage,
    EmailParticipant,
    Lead,
    Note,
    Person,
    PhoneEvent,
    User,
)
from app.services.common import display_name_map, entity_labels_map, row_to_dict
from app.services.google import normalize_email
from app.services.ringcentral import normalize_phone

router = APIRouter()

# The feed labels emails/calls with matching people and leads. Participant
# emails are stored normalized (gmail dots and +suffixes stripped), so the
# match must run through normalize_email on the record side too — that can't
# be pushed into SQL against the raw columns. Instead of loading the whole
# contact book on every request (43k rows post-import), build the lookup maps
# once and reuse them briefly; slightly stale chips are invisible, a full
# table scan per page view is not.
_MAPS_TTL_SECONDS = 120
_maps_cache: dict[uuid.UUID, tuple[float, dict, dict]] = {}

# Deep pages are OFFSET scans — cheap skinny rows now, but still work that
# grows with the page number, and no reader scrolls that far: the iOS app
# stops at 10 pages and points at the web app for older history. Past the cap
# the feed just ends.
_MAX_PAGE = 200


async def _org_contact_maps(db: AsyncSession, org_id: uuid.UUID) -> tuple[dict, dict]:
    """(addr_map, phone_map): normalized email/number -> (type, id, label)."""
    cached = _maps_cache.get(org_id)
    if cached and time.monotonic() - cached[0] < _MAPS_TTL_SECONDS:
        return cached[1], cached[2]

    addr_map: dict[str, tuple[str, uuid.UUID, str]] = {}
    phone_map: dict[str, tuple[str, uuid.UUID, str]] = {}

    people = await db.execute(
        select(Person.id, Person.first_name, Person.last_name, Person.work_email,
               Person.personal_email, Person.work_phone, Person.mobile_phone).where(
            Person.org_id == org_id, Person.deleted_at.is_(None)
        )
    )
    for pid, first, last, work, personal, wphone, mphone in people:
        label = " ".join(filter(None, [first, last]))
        for e in (work, personal):
            if e:
                addr_map.setdefault(normalize_email(e), ("person", pid, label))
        for p in (wphone, mphone):
            n = normalize_phone(p)
            if n:
                phone_map.setdefault(n, ("person", pid, label))

    leads = await db.execute(
        select(Lead.id, Lead.first_name, Lead.last_name, Lead.email, Lead.work_phone,
               Lead.mobile_phone).where(Lead.org_id == org_id, Lead.deleted_at.is_(None))
    )
    for lid, first, last, email, wphone, mphone in leads:
        label = " ".join(filter(None, [first, last]))
        if email:
            addr_map.setdefault(normalize_email(email), ("lead", lid, label))
        for p in (wphone, mphone):
            n = normalize_phone(p)
            if n:
                phone_map.setdefault(n, ("lead", lid, label))

    _maps_cache[org_id] = (time.monotonic(), addr_map, phone_map)
    return addr_map, phone_map


def _timeline(org_id: uuid.UUID, kind: str, offset: int, limit: int):
    """One statement for the whole feed's ORDER: a UNION ALL of skinny
    (kind, id, ts) rows from the sources `kind` selects, sorted and paged by
    the database. Nothing wide is read here — the page's rows are hydrated
    afterwards, by id.

    The order is total — (ts DESC, kind, id) — so items sharing a timestamp
    (a bulk import stamps hundreds) can't shuffle between two page requests.

    Each source is pre-trimmed to its own newest offset+limit rows: no row
    beyond that can reach this page, and it lets Postgres walk the
    (org_id, created_at) indexes instead of sorting whole tables."""
    engaged = exists().where(
        (EmailParticipant.email_id == EmailMessage.id) & EmailParticipant.direct.is_(True)
    )
    email_ts = func.coalesce(EmailMessage.sent_at, EmailMessage.created_at)
    phone_ts = func.coalesce(PhoneEvent.happened_at, PhoneEvent.created_at)
    sources = {
        "email": select(
            literal("email").label("kind"), EmailMessage.id.label("id"), email_ts.label("ts")
        ).where(EmailMessage.org_id == org_id, engaged),
        "phone": select(
            literal("phone").label("kind"), PhoneEvent.id.label("id"), phone_ts.label("ts")
        ).where(PhoneEvent.org_id == org_id),
        "note": select(
            literal("note").label("kind"), Note.id.label("id"), Note.created_at.label("ts")
        ).where(Note.org_id == org_id),
        "activity": select(
            literal("activity").label("kind"),
            Activity.id.label("id"),
            Activity.created_at.label("ts"),
        ).where(Activity.org_id == org_id),
    }
    reach = offset + limit
    parts = []
    for name, stmt in sources.items():
        if kind not in ("all", name):
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


async def _hydrate_emails(db, org_id, ids: list) -> dict:
    # Skinny columns only — the cached bodies can be tens of KB per row.
    emails = (
        await db.execute(
            select(
                EmailMessage.id,
                EmailMessage.sent_at,
                EmailMessage.created_at,
                EmailMessage.subject,
                EmailMessage.snippet,
                EmailMessage.from_email,
                EmailMessage.from_name,
                EmailMessage.is_outgoing,
                EmailMessage.owner_user_id,
                EmailMessage.gmail_id,
            ).where(EmailMessage.org_id == org_id, EmailMessage.id.in_(ids))
        )
    ).all()
    # who in the CRM each email touches, via engaged participants
    related: dict[uuid.UUID, list[dict]] = {}
    parts = await db.execute(
        select(EmailParticipant.email_id, EmailParticipant.email).where(
            EmailParticipant.email_id.in_(ids),
            EmailParticipant.direct.is_(True),
        )
    )
    by_addr: dict[str, list[uuid.UUID]] = {}
    for eid, addr in parts:
        by_addr.setdefault(addr, []).append(eid)
    addr_map, _ = await _org_contact_maps(db, org_id)
    for addr, eids in by_addr.items():
        hit = addr_map.get(addr)
        if not hit:
            continue
        etype, eid_, label = hit
        for eid in eids:
            entry = {"entity_type": etype, "entity_id": str(eid_), "label": label}
            bucket = related.setdefault(eid, [])
            if entry not in bucket:
                bucket.append(entry)
    return {
        e.id: {
            "type": "email",
            "at": (e.sent_at or e.created_at).isoformat(),
            "id": str(e.id),
            "subject": e.subject,
            "snippet": e.snippet,
            "from_email": e.from_email,
            "from_name": e.from_name,
            "is_outgoing": e.is_outgoing,
            "owner_user_id": str(e.owner_user_id) if e.owner_user_id else None,
            "gmail_id": e.gmail_id,
            "related": related.get(e.id, []),
        }
        for e in emails
    }


async def _hydrate_phone(db, org_id, ids: list) -> dict:
    events = (
        (
            await db.execute(
                select(PhoneEvent).where(PhoneEvent.org_id == org_id, PhoneEvent.id.in_(ids))
            )
        )
        .scalars()
        .all()
    )
    phone_map: dict[str, tuple[str, uuid.UUID, str]] = {}
    if events:
        _, phone_map = await _org_contact_maps(db, org_id)
    out = {}
    for e in events:
        # A manually logged event carries its own record link; its label is
        # resolved with the other entity labels. Synced ones match by number.
        hit = None if (e.entity_type and e.entity_id) else phone_map.get(e.other_number)
        out[e.id] = {
            "type": e.kind,  # call | sms
            "at": (e.happened_at or e.created_at).isoformat(),
            "id": str(e.id),
            "direction": e.direction,
            "other_number": e.other_number,
            "other_name": e.other_name,
            "duration_seconds": e.duration_seconds,
            "result": e.result,
            "text": e.text,
            "recording_id": e.recording_id,
            "entity_type": e.entity_type,
            "entity_id": str(e.entity_id) if e.entity_id else None,
            "related": [{"entity_type": hit[0], "entity_id": str(hit[1]), "label": hit[2]}]
            if hit
            else [],
        }
    return out


async def _hydrate_notes(db, org_id, ids: list) -> dict:
    notes = (
        (await db.execute(select(Note).where(Note.org_id == org_id, Note.id.in_(ids))))
        .scalars()
        .all()
    )
    authors = await display_name_map(db, {n.author_id for n in notes}, org_id)
    out = {}
    for n in notes:
        d = row_to_dict(n)
        out[n.id] = {
            "type": "note",
            "at": d["created_at"],
            "id": d["id"],
            "body": d["body"],
            "author_name": authors.get(d.get("author_id")),
            "entity_type": d["entity_type"],
            "entity_id": d["entity_id"],
        }
    return out


async def _hydrate_activities(db, org_id, ids: list) -> dict:
    acts = (
        (
            await db.execute(
                select(Activity).where(Activity.org_id == org_id, Activity.id.in_(ids))
            )
        )
        .scalars()
        .all()
    )
    actors = await display_name_map(db, {a.actor_id for a in acts}, org_id)
    out = {}
    for a in acts:
        d = row_to_dict(a)
        out[a.id] = {
            "type": "activity",
            "at": d["created_at"],
            "id": d["id"],
            "kind": d["kind"],
            "payload": d["payload"],
            "actor_name": actors.get(d.get("actor_id")),
            "entity_type": d["entity_type"],
            "entity_id": d["entity_id"],
        }
    return out


_HYDRATORS = {
    "email": _hydrate_emails,
    "phone": _hydrate_phone,
    "note": _hydrate_notes,
    "activity": _hydrate_activities,
}


@router.get("")
async def feed(
    page: int = 1,
    page_size: int = 30,
    kind: str = "all",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    page = max(1, page)
    page_size = min(max(page_size, 1), 100)
    if page > _MAX_PAGE:
        # Past the cap: an empty last page, not an error. The clients page by
        # number and would surface a 4xx as a failure; an empty page with
        # has_more false just ends the scroll.
        return {"items": [], "page": page, "page_size": page_size, "has_more": False}

    # One extra row tells us whether another page exists.
    stmt = _timeline(user.org_id, kind, (page - 1) * page_size, page_size + 1)
    order = (await db.execute(stmt)).all() if stmt is not None else []
    has_more = len(order) > page_size
    order = order[:page_size]

    # Hydrate just this page: one bounded lookup per source that appears on
    # it. The work is the same on page 1 and page 150.
    ids_by_source: dict[str, list] = {}
    for source, rid, _ts in order:
        ids_by_source.setdefault(source, []).append(rid)
    hydrated: dict[str, dict] = {}
    for source, ids in ids_by_source.items():
        hydrated[source] = await _HYDRATORS[source](db, user.org_id, ids)
    page_items = [
        hydrated[source][rid] for source, rid, _ts in order if rid in hydrated[source]
    ]

    refs = {
        (i.get("entity_type"), i.get("entity_id"))
        for i in page_items
        if i["type"] in ("note", "activity", "call", "sms")
    }
    labels = await entity_labels_map(db, user.org_id, refs, unnamed="(unnamed)")
    for i in page_items:
        if i["type"] in ("call", "sms") and i.get("entity_type") and i.get("entity_id") and not i["related"]:
            label = labels.get((i["entity_type"], i["entity_id"]))
            if label:
                i["related"] = [
                    {"entity_type": i["entity_type"], "entity_id": i["entity_id"], "label": label}
                ]
        if i["type"] in ("note", "activity") and i.get("entity_type") and i.get("entity_id"):
            label = labels.get((i["entity_type"], i["entity_id"]))
            i["related"] = (
                [{"entity_type": i["entity_type"], "entity_id": i["entity_id"], "label": label}]
                if label
                else []
            )
        i.setdefault("related", [])

    return {"items": page_items, "page": page, "page_size": page_size, "has_more": has_more}
