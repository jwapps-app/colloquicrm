"""The record types, and what hangs off them.

Notes, tasks, tags, attachments and the rest point at a record through an
untyped (entity_type, entity_id) pair — there is no foreign key, so nothing
in the database knows which types exist or what should happen to those rows
when a record is merged away or permanently deleted. This module is where
that is written down, once:

  RECORDS      the record types (person, lead, company, opportunity)
  SATELLITES   every table that hangs off a record, with what a merge and a
               permanent delete do to its rows

A new record type is one entry in RECORDS. A new table that points at
records is one entry in SATELLITES — merge and purge both read it, so
neither can be forgotten.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Activity,
    Attachment,
    Company,
    CustomFieldValue,
    EntityTag,
    Lead,
    Note,
    Opportunity,
    Person,
    PhoneEvent,
    Task,
)
from app.services.contacts import CORRESPONDENT_TYPES


@dataclass(frozen=True)
class RecordType:
    name: str  # the entity_type stored in satellite rows
    model: type
    plural: str  # URL segment, import type and export file name

    @property
    def correspondent(self) -> bool:
        """Has an email / call / calendar timeline (see services/contacts)."""
        return self.name in CORRESPONDENT_TYPES


RECORDS: dict[str, RecordType] = {
    r.name: r
    for r in (
        RecordType("person", Person, "people"),
        RecordType("lead", Lead, "leads"),
        RecordType("company", Company, "companies"),
        RecordType("opportunity", Opportunity, "opportunities"),
    )
}
RECORD_TYPES = frozenset(RECORDS)
BY_PLURAL: dict[str, RecordType] = {r.plural: r for r in RECORDS.values()}

# Things a feed row or automation rule can point AT without being a record
# that satellites hang off.
LABELLED_MODELS: dict[str, type] = {**{n: r.model for n, r in RECORDS.items()}, "task": Task}


def record_model(entity_type: str | None):
    """The model for a record type, or None when the type is unknown."""
    rec = RECORDS.get(entity_type or "")
    return rec.model if rec else None


def plural(entity_type: str) -> str:
    rec = RECORDS.get(entity_type)
    return rec.plural if rec else f"{entity_type}s"


async def get_record(
    db: AsyncSession,
    org_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    *,
    include_trashed: bool = True,
):
    """One in-org record by type and id, or None. Raises KeyError for a type
    that isn't a record type."""
    model = RECORDS[entity_type].model
    conds = [model.id == entity_id, model.org_id == org_id]
    if not include_trashed:
        conds.append(model.deleted_at.is_(None))
    return (await db.execute(select(model).where(*conds))).scalar_one_or_none()


# ---- satellites ----

# What a merge does with the duplicate's rows:
MOVE = "move"  # re-point at the survivor
MOVE_UNLESS_TAKEN = "move_unless_taken"  # re-point, unless the survivor has its own
# Give the survivor its own copy and leave the duplicate's in place, so a
# duplicate restored from Trash still has them. Only for tables whose rows
# are nothing but the key and the pointer.
COPY_UNLESS_TAKEN = "copy_unless_taken"
# What a permanent delete of the record does with its rows:
DELETE = "delete"
DETACH = "detach"  # keep the row, null both halves of the pointer
KEEP = "keep"  # leave untouched (the audit trail)


@dataclass(frozen=True)
class Satellite:
    model: type
    on_merge: str
    on_purge: str
    # Column that makes a row "the same thing" on two records; read for the
    # *_UNLESS_TAKEN merges.
    unique_per_record: str | None = None


SATELLITES: tuple[Satellite, ...] = (
    Satellite(Note, MOVE, DELETE),
    Satellite(Task, MOVE, DETACH),
    Satellite(Activity, MOVE, KEEP),
    # Only manually logged events carry a record pointer; synced ones are
    # matched by number and belong to no record in particular.
    Satellite(PhoneEvent, MOVE, DELETE),
    Satellite(Attachment, MOVE, DELETE),
    Satellite(EntityTag, COPY_UNLESS_TAKEN, DELETE, unique_per_record="tag_id"),
    Satellite(CustomFieldValue, MOVE_UNLESS_TAKEN, DELETE, unique_per_record="field_id"),
)


async def move_satellites(
    db: AsyncSession, entity_type: str, source_id: uuid.UUID, target_id: uuid.UUID
) -> None:
    """A merge: everything hanging off `source_id` follows the survivor.
    Rows the survivor already has its own version of (the same tag, a value
    for the same custom field) stay behind with the source."""
    for sat in SATELLITES:
        m = sat.model
        mine = (m.entity_type == entity_type) & (m.entity_id == source_id)
        if sat.on_merge == MOVE:
            await db.execute(update(m).where(mine).values(entity_id=target_id))
            continue
        key = getattr(m, sat.unique_per_record)
        taken = {
            k
            for (k,) in await db.execute(
                select(key).where(m.entity_type == entity_type, m.entity_id == target_id)
            )
        }
        free = [k for (k,) in await db.execute(select(key).where(mine)) if k not in taken]
        if not free:
            continue
        if sat.on_merge == COPY_UNLESS_TAKEN:
            await db.execute(
                insert(m),
                [
                    {sat.unique_per_record: k, "entity_type": entity_type, "entity_id": target_id}
                    for k in free
                ],
            )
        else:
            await db.execute(update(m).where(mine, key.in_(free)).values(entity_id=target_id))


async def purge_satellites(
    db: AsyncSession, entity_type: str, entity_ids: list[uuid.UUID]
) -> list[str]:
    """A permanent delete: apply each satellite's on_purge decision to the
    rows of these records. Returns the stored_names of deleted attachments —
    the caller unlinks those files AFTER its commit."""
    if not entity_ids:
        return []
    stored: list[str] = []
    for sat in SATELLITES:
        m = sat.model
        mine = (m.entity_type == entity_type) & (m.entity_id.in_(entity_ids))
        if sat.on_purge == KEEP:
            continue
        if sat.on_purge == DETACH:
            await db.execute(update(m).where(mine).values(entity_type=None, entity_id=None))
            continue
        if m is Attachment:
            stored = [n for (n,) in await db.execute(select(m.stored_name).where(mine))]
        # (A note elsewhere that documented a deleted call keeps its text and
        # loses only the link: notes.phone_event_id is ON DELETE SET NULL.)
        await db.execute(delete(m).where(mine))
    return stored
