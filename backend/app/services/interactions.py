"""Copper-style relationship metrics: a person's interaction count and
last-contacted date, computed from every engaged touchpoint we sync —
direct emails, calls, and texts.

Recomputation is set-based: whatever set of people a caller hands over is
answered by one grouped query per source (emails, phone events), not a pair
of queries per person — a backfill touches the same contacts over and over.

last_contacted_at is the later of the date derived from those events and the
date an import carried (last_contacted_imported_at), so a sync never loses an
imported date and an imported date never hides a newer email.
"""

import uuid

from sqlalchemy import String, Uuid, bindparam, column, func, select, union, update, values

from app.models import EmailMessage, EmailParticipant, Person, PhoneEvent
from app.services.contacts import address_fields, record_emails, record_numbers

# People per round of grouped queries. Two addresses and two numbers each
# keeps a round far inside asyncpg's 32767 bind-parameter ceiling.
_CHUNK = 500


async def _email_aggregates(db, org_id: uuid.UUID, pairs: list[tuple[uuid.UUID, str]]) -> dict:
    """{person_id: (count, latest)} over direct email participation, for
    (person_id, normalized address) pairs."""
    if not pairs:
        return {}
    addr = (
        values(column("person_id", Uuid), column("email", String), name="addr")
        .data(pairs)
        .cte("person_addr")
    )
    rows = await db.execute(
        select(
            addr.c.person_id,
            func.count(func.distinct(EmailMessage.id)),
            func.max(EmailMessage.sent_at),
        )
        .select_from(addr)
        .join(EmailParticipant, EmailParticipant.email == addr.c.email)
        .join(EmailMessage, EmailMessage.id == EmailParticipant.email_id)
        .where(EmailMessage.org_id == org_id, EmailParticipant.direct.is_(True))
        .group_by(addr.c.person_id)
    )
    return {pid: (count, latest) for pid, count, latest in rows}


async def _phone_aggregates(
    db, org_id: uuid.UUID, person_ids: list[uuid.UUID], pairs: list[tuple[uuid.UUID, str]]
) -> dict:
    """{person_id: (count, latest)} over calls/texts: events matched by one
    of the person's numbers, plus events logged directly on the person. The
    UNION folds an event that qualifies both ways into one."""
    linked = select(
        PhoneEvent.entity_id.label("person_id"),
        PhoneEvent.id.label("event_id"),
        PhoneEvent.happened_at.label("happened_at"),
    ).where(
        PhoneEvent.org_id == org_id,
        PhoneEvent.entity_type == "person",
        PhoneEvent.entity_id.in_(person_ids),
    )
    if pairs:
        num = (
            values(column("person_id", Uuid), column("number", String), name="num")
            .data(pairs)
            .cte("person_num")
        )
        by_number = (
            select(
                num.c.person_id.label("person_id"),
                PhoneEvent.id.label("event_id"),
                PhoneEvent.happened_at.label("happened_at"),
            )
            .select_from(num)
            .join(PhoneEvent, PhoneEvent.other_number == num.c.number)
            .where(PhoneEvent.org_id == org_id)
        )
        events = union(linked, by_number).subquery("person_events")
    else:
        events = linked.subquery("person_events")
    rows = await db.execute(
        select(
            events.c.person_id, func.count(events.c.event_id), func.max(events.c.happened_at)
        ).group_by(events.c.person_id)
    )
    return {pid: (count, latest) for pid, count, latest in rows}


async def update_person_aggregates(db, org_id: uuid.UUID, person_ids: set[uuid.UUID]) -> None:
    if not person_ids:
        return
    await db.flush()  # pending rows must be visible (autoflush is off)
    table = Person.__table__
    # Maintenance, not a human touch: the stale-record automations read
    # updated_at as "someone worked this record", so these writes go through
    # Core and pin updated_at to itself — leaving it out of SET would let the
    # column's onupdate stamp it anyway.
    set_both = (
        update(table)
        .where(table.c.id == bindparam("pid"))
        .values(
            interaction_count=bindparam("n"),
            last_contacted_at=bindparam("latest"),
            updated_at=table.c.updated_at,
        )
    )
    set_count = (
        update(table)
        .where(table.c.id == bindparam("pid"))
        .values(interaction_count=bindparam("n"), updated_at=table.c.updated_at)
    )

    ids = list(person_ids)
    for i in range(0, len(ids), _CHUNK):
        people = (
            await db.execute(
                select(
                    Person.id,
                    *[getattr(Person, f) for f in address_fields("person")],
                    Person.interaction_count,
                    Person.last_contacted_imported_at,
                ).where(
                    Person.id.in_(ids[i : i + _CHUNK]),
                    Person.org_id == org_id,
                    Person.deleted_at.is_(None),
                )
            )
        ).all()
        if not people:
            continue
        email_pairs: set[tuple[uuid.UUID, str]] = set()
        phone_pairs: set[tuple[uuid.UUID, str]] = set()
        for p in people:
            email_pairs.update((p.id, e) for e in record_emails("person", p))
            phone_pairs.update((p.id, n) for n in record_numbers("person", p))
        by_email = await _email_aggregates(db, org_id, sorted(email_pairs, key=str))
        by_phone = await _phone_aggregates(
            db, org_id, [p.id for p in people], sorted(phone_pairs, key=str)
        )

        both: list[dict] = []
        count_only: list[dict] = []
        for p in people:
            email_count, email_latest = by_email.get(p.id, (0, None))
            phone_count, phone_latest = by_phone.get(p.id, (0, None))
            total = (email_count or 0) + (phone_count or 0)
            # The later of what the events say and what an import said.
            latest = max(
                (
                    d
                    for d in (email_latest, phone_latest, p.last_contacted_imported_at)
                    if d is not None
                ),
                default=None,
            )
            if latest is not None or (p.interaction_count or 0) > 0:
                # Events exist, or did until now. A date derived from events
                # that are gone (deleted call, address removed) falls back to
                # the imported date, and is cleared only when there is none.
                both.append({"pid": p.id, "n": total, "latest": latest})
            else:
                # No events now, none counted before, nothing imported:
                # whatever date is on the record is a manual edit — the only
                # history there is, and not ours to erase.
                count_only.append({"pid": p.id, "n": total})
        if both:
            await db.execute(set_both, both)
        if count_only:
            await db.execute(set_count, count_only)

        # Core writes bypass the identity map. Refresh any of these people
        # the session already holds (a PATCH or merge serializes its object
        # right after this) without marking them dirty — a dirty flag would
        # flush an ORM UPDATE and bump updated_at after all.
        from sqlalchemy.orm.attributes import set_committed_value

        identity_map = db.sync_session.identity_map
        for row in both + count_only:
            obj = identity_map.get(Person.__mapper__.identity_key_from_primary_key((row["pid"],)))
            if obj is None:
                continue
            set_committed_value(obj, "interaction_count", row["n"])
            if "latest" in row:
                set_committed_value(obj, "last_contacted_at", row["latest"])


async def recompute_in_batches(org_id: uuid.UUID, person_ids, batch: int = 200) -> None:
    """Recompute a large set of people in short transactions of its own —
    for callers (an import job) that must not hold one open across the lot."""
    from app.db import SessionLocal

    ids = list(person_ids)
    async with SessionLocal() as db:
        for i in range(0, len(ids), batch):
            await update_person_aggregates(db, org_id, set(ids[i : i + batch]))
            await db.commit()
