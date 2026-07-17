import uuid
from datetime import date, datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import delete, inspect as sa_inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity, CustomField, CustomFieldValue, EntityTag, Person, Tag, User


def entity_label(obj) -> str:
    """Human label for any CRM record: companies and opportunities carry a
    `name`; people and leads a first/last name. Empty string when neither
    yields anything (callers supply their own fallback)."""
    return getattr(obj, "name", None) or " ".join(
        filter(None, [getattr(obj, "first_name", None), getattr(obj, "last_name", None)])
    )


def row_to_dict(obj) -> dict:
    out = {}
    for attr in sa_inspect(obj).mapper.column_attrs:
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
        if value in (None, ""):
            if existing is not None:
                await db.delete(existing)
        elif existing is not None:
            existing.value = str(value)
        else:
            db.add(
                CustomFieldValue(
                    field_id=field_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    value=str(value),
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


ENTITY_MODELS = {
    "person": "Person",
    "company": "Company",
    "opportunity": "Opportunity",
    "lead": "Lead",
}


async def company_people(db: AsyncSession, org_id: uuid.UUID, company_id: uuid.UUID):
    """Non-deleted people linked to a company, with just the columns needed to
    roll their correspondence (emails + phone events) up onto the company
    timeline. One query; caller normalizes the emails/numbers it uses."""
    rows = await db.execute(
        select(
            Person.id,
            Person.work_email,
            Person.personal_email,
            Person.work_phone,
            Person.mobile_phone,
        ).where(
            Person.org_id == org_id,
            Person.company_id == company_id,
            Person.deleted_at.is_(None),
        )
    )
    return rows.all()


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
    import app.models as models

    name = ENTITY_MODELS.get(entity_type)
    if name is None:
        raise HTTPException(status_code=422, detail=f"Unknown entity type: {entity_type}")
    model = getattr(models, name)
    conds = [model.id == entity_id, model.org_id == org_id]
    if hasattr(model, "deleted_at"):
        conds.append(model.deleted_at.is_(None))
    exists = (await db.execute(select(model.id).where(*conds))).first()
    if exists is None:
        raise HTTPException(status_code=404, detail=f"{entity_type} not found")


async def cleanup_entity(db: AsyncSession, entity_type: str, entity_id: uuid.UUID) -> None:
    """Remove polymorphic satellites (tags, custom values, notes) when an
    entity is deleted. Activities are kept as an audit trail."""
    from app.models import Note

    await db.execute(
        delete(EntityTag).where(
            EntityTag.entity_type == entity_type, EntityTag.entity_id == entity_id
        )
    )
    await db.execute(
        delete(CustomFieldValue).where(
            CustomFieldValue.entity_type == entity_type,
            CustomFieldValue.entity_id == entity_id,
        )
    )
    await db.execute(
        delete(Note).where(Note.entity_type == entity_type, Note.entity_id == entity_id)
    )
