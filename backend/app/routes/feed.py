import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import (
    Activity,
    Company,
    EmailMessage,
    EmailParticipant,
    Lead,
    Note,
    Opportunity,
    Person,
    PhoneEvent,
    Task,
    User,
)
from app.services.common import display_name_map, row_to_dict
from app.services.google import normalize_email
from app.services.ringcentral import normalize_phone

router = APIRouter()


async def _entity_labels(db: AsyncSession, org_id: uuid.UUID, refs: set) -> dict:
    """(entity_type, id) -> display label, batched per type."""
    out: dict = {}
    by_type: dict[str, list[uuid.UUID]] = {}
    for etype, eid in refs:
        if etype and eid:
            by_type.setdefault(etype, []).append(uuid.UUID(str(eid)))
    for etype, model, label_cols in (
        ("person", Person, None),
        ("lead", Lead, None),
        ("company", Company, Company.name),
        ("opportunity", Opportunity, Opportunity.name),
        ("task", Task, Task.name),
    ):
        ids = by_type.get(etype)
        if not ids:
            continue
        if label_cols is None:
            rows = await db.execute(
                select(model.id, model.first_name, model.last_name).where(
                    model.org_id == org_id, model.id.in_(ids)
                )
            )
            for rid, first, last in rows:
                out[(etype, str(rid))] = " ".join(filter(None, [first, last])) or "(unnamed)"
        else:
            rows = await db.execute(
                select(model.id, label_cols).where(model.org_id == org_id, model.id.in_(ids))
            )
            for rid, name in rows:
                out[(etype, str(rid))] = name
    return out


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
    need = page * page_size + 1  # one extra to detect has_more

    items: list[dict] = []

    if kind in ("all", "email"):
        emails = (
            (
                await db.execute(
                    select(EmailMessage)
                    .join(EmailParticipant, EmailParticipant.email_id == EmailMessage.id)
                    .where(
                        EmailMessage.org_id == user.org_id,
                        EmailParticipant.direct.is_(True),
                    )
                    .distinct()
                    .order_by(EmailMessage.sent_at.desc())
                    .limit(need)
                )
            )
            .scalars()
            .all()
        )
        email_ids = [e.id for e in emails]
        related: dict[uuid.UUID, list[dict]] = {}
        if email_ids:
            # who in the CRM each email touches, via engaged participants
            parts = await db.execute(
                select(EmailParticipant.email_id, EmailParticipant.email).where(
                    EmailParticipant.email_id.in_(email_ids),
                    EmailParticipant.direct.is_(True),
                )
            )
            by_addr: dict[str, list[uuid.UUID]] = {}
            for eid, addr in parts:
                by_addr.setdefault(addr, []).append(eid)
            addr_map: dict[str, tuple[str, uuid.UUID, str]] = {}
            people = await db.execute(
                select(Person.id, Person.first_name, Person.last_name, Person.work_email,
                       Person.personal_email).where(Person.org_id == user.org_id)
            )
            for pid, first, last, work, personal in people:
                label = " ".join(filter(None, [first, last]))
                for e in (work, personal):
                    if e:
                        addr_map.setdefault(normalize_email(e), ("person", pid, label))
            leads = await db.execute(
                select(Lead.id, Lead.first_name, Lead.last_name, Lead.email).where(
                    Lead.org_id == user.org_id, Lead.email.is_not(None)
                )
            )
            for lid, first, last, e in leads:
                addr_map.setdefault(
                    normalize_email(e), ("lead", lid, " ".join(filter(None, [first, last])))
                )
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
        for e in emails:
            items.append(
                {
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
            )

    if kind in ("all", "phone"):
        events = (
            (
                await db.execute(
                    select(PhoneEvent)
                    .where(PhoneEvent.org_id == user.org_id)
                    .order_by(PhoneEvent.happened_at.desc())
                    .limit(need)
                )
            )
            .scalars()
            .all()
        )
        phone_map: dict[str, tuple[str, uuid.UUID, str]] = {}
        if events:
            people = await db.execute(
                select(Person.id, Person.first_name, Person.last_name, Person.work_phone,
                       Person.mobile_phone).where(Person.org_id == user.org_id)
            )
            for pid, first, last, work, mobile in people:
                label = " ".join(filter(None, [first, last]))
                for p in (work, mobile):
                    n = normalize_phone(p)
                    if n:
                        phone_map.setdefault(n, ("person", pid, label))
            leads = await db.execute(
                select(Lead.id, Lead.first_name, Lead.last_name, Lead.work_phone,
                       Lead.mobile_phone).where(Lead.org_id == user.org_id)
            )
            for lid, first, last, work, mobile in leads:
                label = " ".join(filter(None, [first, last]))
                for p in (work, mobile):
                    n = normalize_phone(p)
                    if n:
                        phone_map.setdefault(n, ("lead", lid, label))
        for e in events:
            hit = phone_map.get(e.other_number)
            items.append(
                {
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
                    "related": [
                        {"entity_type": hit[0], "entity_id": str(hit[1]), "label": hit[2]}
                    ]
                    if hit
                    else [],
                }
            )

    if kind in ("all", "note"):
        notes = (
            (
                await db.execute(
                    select(Note)
                    .where(Note.org_id == user.org_id)
                    .order_by(Note.created_at.desc())
                    .limit(need)
                )
            )
            .scalars()
            .all()
        )
        authors = await display_name_map(db, {n.author_id for n in notes})
        for n in notes:
            d = row_to_dict(n)
            items.append(
                {
                    "type": "note",
                    "at": d["created_at"],
                    "id": d["id"],
                    "body": d["body"],
                    "author_name": authors.get(d.get("author_id")),
                    "entity_type": d["entity_type"],
                    "entity_id": d["entity_id"],
                }
            )

    if kind in ("all", "activity"):
        acts = (
            (
                await db.execute(
                    select(Activity)
                    .where(Activity.org_id == user.org_id)
                    .order_by(Activity.created_at.desc())
                    .limit(need)
                )
            )
            .scalars()
            .all()
        )
        actors = await display_name_map(db, {a.actor_id for a in acts})
        for a in acts:
            d = row_to_dict(a)
            items.append(
                {
                    "type": "activity",
                    "at": d["created_at"],
                    "id": d["id"],
                    "kind": d["kind"],
                    "payload": d["payload"],
                    "actor_name": actors.get(d.get("actor_id")),
                    "entity_type": d["entity_type"],
                    "entity_id": d["entity_id"],
                }
            )

    items.sort(key=lambda x: x["at"] or "", reverse=True)
    start = (page - 1) * page_size
    page_items = items[start : start + page_size]
    has_more = len(items) > start + page_size

    refs = {
        (i.get("entity_type"), i.get("entity_id"))
        for i in page_items
        if i["type"] in ("note", "activity")
    }
    labels = await _entity_labels(db, user.org_id, refs)
    for i in page_items:
        if i["type"] in ("note", "activity") and i.get("entity_type") and i.get("entity_id"):
            label = labels.get((i["entity_type"], i["entity_id"]))
            i["related"] = (
                [{"entity_type": i["entity_type"], "entity_id": i["entity_id"], "label": label}]
                if label
                else []
            )
        i.setdefault("related", [])

    return {"items": page_items, "page": page, "page_size": page_size, "has_more": has_more}
