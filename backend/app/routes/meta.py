import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user, require_admin
from app.models import CustomField, CustomFieldValue, EntityTag, SavedFilter, Tag, User
from app.schemas import (
    CustomFieldIn,
    CustomFieldUpdateIn,
    SavedFilterIn,
    SavedFilterUpdateIn,
)
from app.services.common import row_to_dict

tags_router = APIRouter()
custom_fields_router = APIRouter()
saved_filters_router = APIRouter()
options_router = APIRouter()

ENTITY_TYPES = {"person", "lead", "company", "opportunity"}

DEFAULT_CONTACT_TYPES = [
    "Potential Customer",
    "Current Customer",
    "Former Customer",
    "Uncategorized",
    "Other",
]


@options_router.get("/contact-types")
async def contact_types(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Every contact type in use across people and companies, plus the
    defaults — imported data decides what the dropdowns offer."""
    from app.models import Company, Person

    values: set[str] = set(DEFAULT_CONTACT_TYPES)
    for model in (Person, Company):
        rows = await db.execute(
            select(model.contact_type)
            .where(model.org_id == user.org_id, model.contact_type.is_not(None))
            .distinct()
        )
        values.update(v for (v,) in rows if v and v.strip())
    return sorted(values)


@tags_router.get("")
async def list_tags(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = await db.execute(
        select(Tag.id, Tag.name, func.count(EntityTag.entity_id))
        .outerjoin(EntityTag, EntityTag.tag_id == Tag.id)
        .where(Tag.org_id == user.org_id)
        .group_by(Tag.id, Tag.name)
        .order_by(Tag.name)
    )
    return [{"id": str(tid), "name": name, "count": count} for tid, name, count in rows]


def _check_entity_type(entity_type: str) -> None:
    if entity_type not in ENTITY_TYPES:
        raise HTTPException(
            status_code=422, detail=f"entity_type must be one of {sorted(ENTITY_TYPES)}"
        )


@custom_fields_router.get("")
async def list_custom_fields(
    entity_type: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(CustomField).where(CustomField.org_id == user.org_id)
    if entity_type:
        stmt = stmt.where(CustomField.entity_type == entity_type)
    stmt = stmt.order_by(CustomField.entity_type, CustomField.position, CustomField.name)
    fields = (await db.execute(stmt)).scalars().all()
    return [row_to_dict(f) for f in fields]


@custom_fields_router.post("", status_code=201)
async def create_custom_field(
    body: CustomFieldIn, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
):
    _check_entity_type(body.entity_type)
    exists = (
        await db.execute(
            select(CustomField).where(
                CustomField.org_id == user.org_id,
                CustomField.entity_type == body.entity_type,
                CustomField.name == body.name,
            )
        )
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=409, detail="A field with that name already exists")
    max_pos = (
        await db.execute(
            select(func.coalesce(func.max(CustomField.position), -1)).where(
                CustomField.org_id == user.org_id, CustomField.entity_type == body.entity_type
            )
        )
    ).scalar_one()
    field = CustomField(
        org_id=user.org_id,
        entity_type=body.entity_type,
        name=body.name,
        field_type=body.field_type,
        options=body.options,
        position=body.position if body.position is not None else max_pos + 1,
    )
    db.add(field)
    await db.flush()
    result = row_to_dict(field)
    await db.commit()  # visible before the client refetches
    return result


async def _get_field(db, user, field_id: uuid.UUID) -> CustomField:
    field = (
        await db.execute(
            select(CustomField).where(
                CustomField.id == field_id, CustomField.org_id == user.org_id
            )
        )
    ).scalar_one_or_none()
    if field is None:
        raise HTTPException(status_code=404, detail="Custom field not found")
    return field


@custom_fields_router.patch("/{field_id}")
async def update_custom_field(
    field_id: uuid.UUID,
    body: CustomFieldUpdateIn,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    field = await _get_field(db, user, field_id)
    for key in ("name", "field_type", "options", "position"):
        value = getattr(body, key)
        if value is not None:
            setattr(field, key, value)
    if body.field_type == "date":
        # Convert existing values (e.g. Copper's 7/6/2026) so date pickers
        # can read them.
        from app.services.importer import _parse_date

        values = (
            (
                await db.execute(
                    select(CustomFieldValue).where(CustomFieldValue.field_id == field.id)
                )
            )
            .scalars()
            .all()
        )
        for v in values:
            parsed = _parse_date(v.value or "")
            if parsed:
                v.value = parsed
    result = row_to_dict(field)
    await db.commit()  # visible before the client refetches
    return result


@custom_fields_router.delete("/{field_id}", status_code=204)
async def delete_custom_field(
    field_id: uuid.UUID,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    field = await _get_field(db, user, field_id)
    await db.delete(field)
    await db.commit()  # visible before the client refetches


@saved_filters_router.get("")
async def list_saved_filters(
    entity_type: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(SavedFilter).where(
        SavedFilter.org_id == user.org_id,
        (SavedFilter.user_id == user.id) | (SavedFilter.is_public.is_(True)),
    )
    if entity_type:
        stmt = stmt.where(SavedFilter.entity_type == entity_type)
    stmt = stmt.order_by(SavedFilter.name)
    filters = (await db.execute(stmt)).scalars().all()
    return [row_to_dict(f) for f in filters]


@saved_filters_router.post("", status_code=201)
async def create_saved_filter(
    body: SavedFilterIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    _check_entity_type(body.entity_type)
    f = SavedFilter(
        org_id=user.org_id,
        user_id=user.id,
        entity_type=body.entity_type,
        name=body.name,
        filters=body.filters,
        is_public=body.is_public,
    )
    db.add(f)
    await db.flush()
    result = row_to_dict(f)
    await db.commit()  # visible before the client refetches
    return result


async def _get_filter(db, user, filter_id: uuid.UUID) -> SavedFilter:
    f = (
        await db.execute(
            select(SavedFilter).where(
                SavedFilter.id == filter_id, SavedFilter.org_id == user.org_id
            )
        )
    ).scalar_one_or_none()
    if f is None:
        raise HTTPException(status_code=404, detail="Saved filter not found")
    if f.user_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="Not your filter")
    return f


@saved_filters_router.patch("/{filter_id}")
async def update_saved_filter(
    filter_id: uuid.UUID,
    body: SavedFilterUpdateIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    f = await _get_filter(db, user, filter_id)
    if body.name is not None:
        f.name = body.name
    if body.filters is not None:
        f.filters = body.filters
    if body.is_public is not None:
        f.is_public = body.is_public
    result = row_to_dict(f)
    await db.commit()  # visible before the client refetches
    return result


@saved_filters_router.delete("/{filter_id}", status_code=204)
async def delete_saved_filter(
    filter_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    f = await _get_filter(db, user, filter_id)
    await db.delete(f)
    await db.commit()  # visible before the client refetches
