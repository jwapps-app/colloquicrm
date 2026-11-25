import uuid
from datetime import date, datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import delete, distinct, exists, inspect as sa_inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import entities

from app.models import (
    Activity,
    CustomField,
    CustomFieldValue,
    EntityTag,
    Tag,
    Task,
    User,
    utcnow,
)


def entity_label(obj) -> str:
    """Human label for any CRM record: companies and opportunities carry a
    `name`; people and leads a first/last name. Empty string when neither
    yields anything (callers supply their own fallback)."""
    return getattr(obj, "name", None) or " ".join(
        filter(None, [getattr(obj, "first_name", None), getattr(obj, "last_name", None)])
    )


def row_to_dict(obj) -> dict:
    """A model instance as the JSON clients get: every column except org_id
    and any declared `mapped_column(..., info={"private": True})`. A new
    column is therefore public unless it says otherwise — tests/test_contract
    pins the resulting key sets, so that is a decision made in review rather
    than by accident."""
    out = {}
    for attr in sa_inspect(obj).mapper.column_attrs:
        if attr.columns[0].info.get("private"):
            continue
        v = getattr(obj, attr.key)
        if isinstance(v, uuid.UUID):
            v = str(v)
        elif isinstance(v, (datetime, date)):
            v = v.isoformat()
        elif isinstance(v, Decimal):
            v = float(v)
        out[attr.key] = v
    out.pop("org_id", None)
    return out


async def get_tag_maps(
    db: AsyncSession, entity_type: str, ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    if not ids:
        return {}
    rows = await db.execute(
        select(EntityTag.entity_id, Tag.name)
        .join(Tag, Tag.id == EntityTag.tag_id)
        .where(EntityTag.entity_type == entity_type, EntityTag.entity_id.in_(ids))
        .order_by(Tag.name)
    )
    out: dict[uuid.UUID, list[str]] = {}
    for eid, name in rows:
        out.setdefault(eid, []).append(name)
    return out


async def get_cf_maps(
    db: AsyncSession, entity_type: str, ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, str | None]]:
    if not ids:
        return {}
    rows = await db.execute(
        select(CustomFieldValue.entity_id, CustomFieldValue.field_id, CustomFieldValue.value).where(
            CustomFieldValue.entity_type == entity_type, CustomFieldValue.entity_id.in_(ids)
        )
    )
    out: dict[uuid.UUID, dict[str, str | None]] = {}
    for eid, fid, value in rows:
        out.setdefault(eid, {})[str(fid)] = value
    return out


async def get_or_create_tag(db: AsyncSession, org_id: uuid.UUID, name: str) -> Tag:
    tag = (
        await db.execute(select(Tag).where(Tag.org_id == org_id, Tag.name == name))
    ).scalar_one_or_none()
    if tag is None:
        tag = Tag(org_id=org_id, name=name)
        db.add(tag)
        await db.flush()
    return tag


async def set_tags(
    db: AsyncSession,
    org_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    names: list[str],
) -> None:
    clean = []
    for n in names:
        n = (n or "").strip()
        if n and n not in clean:
            clean.append(n)
    await db.execute(
        delete(EntityTag).where(
            EntityTag.entity_type == entity_type, EntityTag.entity_id == entity_id
        )
    )
    for name in clean:
        tag = await get_or_create_tag(db, org_id, name)
        db.add(EntityTag(tag_id=tag.id, entity_type=entity_type, entity_id=entity_id))
    # The delete above ran against the DB while the re-adds are still pending —
    # flush so anything reading tags later in this transaction (the PATCH
    # response serializer) sees the new state, not an empty window.
    await db.flush()
    await prune_orphan_tags(db, org_id)


async def prune_orphan_tags(db: AsyncSession, org_id: uuid.UUID | None = None) -> None:
    """Drop tags nothing references anymore, so removing a tag's last use also
    removes it from the org's tag list — there is no manage-tags UI to clean
    them up by hand. Org-scoped normally; org_id=None sweeps all orgs (purge)."""
    stmt = delete(Tag).where(
        ~exists(select(EntityTag.tag_id).where(EntityTag.tag_id == Tag.id))
    )
    if org_id is not None:
        stmt = stmt.where(Tag.org_id == org_id)
    await db.execute(stmt)


async def add_tags(
    db: AsyncSession,
    org_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    names: list[str],
) -> None:
    existing = {
        n
        for (n,) in await db.execute(
            select(Tag.name)
            .join(EntityTag, EntityTag.tag_id == Tag.id)
            .where(EntityTag.entity_type == entity_type, EntityTag.entity_id == entity_id)
        )
    }
    for name in names:
        name = (name or "").strip()
        if not name or name in existing:
            continue
        tag = await get_or_create_tag(db, org_id, name)
        db.add(EntityTag(tag_id=tag.id, entity_type=entity_type, entity_id=entity_id))
        existing.add(name)


# Custom-field values are strings on the wire and in the table. A checkbox is
# the one type where the spelling matters: every client decides checked-ness
# from the string, so there is exactly one spelling of each state.
CHECKBOX_TRUE = frozenset({"true", "1", "yes", "y", "on", "t", "checked"})
CHECKBOX_FALSE = frozenset({"false", "0", "no", "n", "off", "f", "unchecked"})


def canonical_checkbox(value) -> str | None:
    """A checkbox value as stored: "true" / "false" (None = no value).

    Accepts what clients actually send — a JSON boolean (iOS, web), the stored
    string echoed back, or a spreadsheet's "Yes"/"0"/"on". An unrecognized
    non-empty string counts as checked, which is how it always displayed."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return "true" if value else "false"
    text = str(value).strip().lower()
    if not text:
        return None
    if text in CHECKBOX_FALSE:
        return "false"
    return "true"


def custom_value_to_str(field_type: str | None, value) -> str | None:
    """The stored form of one custom-field value; None means "clear it"."""
    if value is None or value == "":
        return None
    if field_type == "checkbox":
        return canonical_checkbox(value)
    if isinstance(value, bool):
        # str(True) is "True" — never let Python's spelling reach the table.
        return "true" if value else "false"
    return str(value)


async def set_custom_fields(
    db: AsyncSession,
    org_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    values: dict,
) -> None:
    for field_key, value in values.items():
        try:
            field_id = uuid.UUID(str(field_key))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid custom field id: {field_key}")
        field = (
            await db.execute(
                select(CustomField).where(
                    CustomField.id == field_id,
                    CustomField.org_id == org_id,
                    CustomField.entity_type == entity_type,
                )
            )
        ).scalar_one_or_none()
        if field is None:
            raise HTTPException(
                status_code=400, detail=f"Unknown custom field for {entity_type}: {field_key}"
            )
        existing = (
            await db.execute(
                select(CustomFieldValue).where(
                    CustomFieldValue.field_id == field_id,
                    CustomFieldValue.entity_id == entity_id,
                )
            )
        ).scalar_one_or_none()
        stored = custom_value_to_str(field.field_type, value)
        if stored is None:
            if existing is not None:
                await db.delete(existing)
        elif existing is not None:
            existing.value = stored
        else:
            db.add(
                CustomFieldValue(
                    field_id=field_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    value=stored,
                )
            )


async def log_activity(
    db: AsyncSession,
    org_id: uuid.UUID,
    entity_type: str | None,
    entity_id: uuid.UUID | None,
    kind: str,
    actor_id: uuid.UUID | None,
    payload: dict | None = None,
) -> None:
    db.add(
        Activity(
            org_id=org_id,
            entity_type=entity_type,
            entity_id=entity_id,
            kind=kind,
            actor_id=actor_id,
            payload=payload or {},
        )
    )


async def display_name_map(
    db: AsyncSession, ids: set, org_id: uuid.UUID
) -> dict[str, str]:
    ids = {uuid.UUID(str(i)) for i in ids if i}
    if not ids:
        return {}
    rows = await db.execute(
        select(User.id, User.display_name).where(
            User.id.in_(ids), User.org_id == org_id
        )
    )
    return {str(uid): name for uid, name in rows}


async def entity_labels_map(
    db: AsyncSession, org_id: uuid.UUID, refs: set, unnamed: str | None = None
) -> dict:
    """(entity_type, id) -> display label, batched one query per type.

    `refs` is a set of (entity_type, entity_id) pairs; entity_id may be a str or
    UUID. Resolves person/lead/company/opportunity/task names in-org, including
    trashed records (a task can still point at a since-deleted record). Keys are
    (entity_type, str(id)); a ref whose record is gone simply won't appear, so
    callers fall back to the type. A record that exists but has no usable name
    labels as `unnamed` (default None). Bounded: at most one SELECT per entity
    type."""
    out: dict = {}
    by_type: dict[str, list[uuid.UUID]] = {}
    for etype, eid in refs:
        if etype and eid:
            try:
                by_type.setdefault(etype, []).append(uuid.UUID(str(eid)))
            except ValueError:
                continue
    for etype, ids in by_type.items():
        model = entities.LABELLED_MODELS.get(etype)
        if model is None:
            continue
        rows = await db.execute(
            select(model).where(model.org_id == org_id, model.id.in_(ids))
        )
        for obj in rows.scalars():
            out[(etype, str(obj.id))] = entity_label(obj) or unnamed
    return out


async def overdue_task_entity_ids(
    db: AsyncSession, org_id: uuid.UUID, entity_type: str, ids: set
) -> set[str]:
    """Subset of `ids` that have at least one open, past-due task hanging off
    them. Used to flag list rows (people/leads) that need attention. `ids` are
    the current page's record ids (str or UUID); returns a set of str ids so a
    caller can test `d["id"] in result`. One bounded SELECT; empty page skips
    the query entirely."""
    uuid_ids = {uuid.UUID(str(i)) for i in ids if i}
    if not uuid_ids:
        return set()
    rows = await db.execute(
        select(distinct(Task.entity_id)).where(
            Task.org_id == org_id,
            Task.entity_type == entity_type,
            Task.status == "open",
            Task.due_at < utcnow(),
            Task.entity_id.in_(uuid_ids),
        )
    )
    return {str(eid) for (eid,) in rows}


async def validate_entity_ref(
    db: AsyncSession,
    org_id: uuid.UUID,
    entity_type: str | None,
    entity_id: uuid.UUID | None,
) -> None:
    """Confirm a polymorphic (entity_type, entity_id) target names a real
    in-org record. Used by notes and tasks so a caller can't hang a note or
    task off another org's record (or a bogus id). Both None = no target."""
    if entity_type is None and entity_id is None:
        return
    if entity_type is None or entity_id is None:
        raise HTTPException(status_code=422, detail="entity_type and entity_id go together")
    if entity_type not in entities.RECORDS:
        raise HTTPException(status_code=422, detail=f"Unknown entity type: {entity_type}")
    record = await entities.get_record(db, org_id, entity_type, entity_id, include_trashed=False)
    if record is None:
        raise HTTPException(status_code=404, detail=f"{entity_type} not found")
