import uuid
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import EntityTag, Tag, User, utcnow
from app.services.common import (
    cleanup_entity,
    get_cf_maps,
    get_tag_maps,
    log_activity,
    row_to_dict,
    set_custom_fields,
    set_tags,
)

Enricher = Callable[[AsyncSession, User, list[dict]], Awaitable[None]]


def register_crud(
    router: APIRouter,
    *,
    model,
    entity_type: str,
    body_model: type[BaseModel],
    search_cols: list,
    sortable: dict,
    filterable: dict,
    default_sort: str,
    required_any: list[str] | None = None,
    has_extras: bool = True,
    enrich: Enricher | None = None,
    after_create: Callable | None = None,
) -> None:
    """Wires list/create/get/patch/delete endpoints for one entity onto a
    router. body_model serves both create and PATCH (exclude_unset)."""

    def base_query(user: User) -> Select:
        return select(model).where(model.org_id == user.org_id)

    async def get_owned(db: AsyncSession, user: User, item_id: uuid.UUID):
        obj = (
            await db.execute(
                select(model).where(model.id == item_id, model.org_id == user.org_id)
            )
        ).scalar_one_or_none()
        if obj is None:
            raise HTTPException(status_code=404, detail=f"{entity_type} not found")
        return obj

    async def serialize(db: AsyncSession, user: User, items: list) -> list[dict]:
        dicts = [row_to_dict(i) for i in items]
        if has_extras:
            ids = [i.id for i in items]
            tag_map = await get_tag_maps(db, entity_type, ids)
            cf_map = await get_cf_maps(db, entity_type, ids)
            for d, item in zip(dicts, items):
                d["tags"] = tag_map.get(item.id, [])
                d["custom_fields"] = cf_map.get(item.id, {})
        if enrich is not None:
            await enrich(db, user, dicts)
        return dicts

    def check_required(data: dict) -> None:
        if not required_any:
            return
        for key in required_any:
            v = data.get(key)
            if isinstance(v, str):
                v = v.strip()
            if v:
                return
        raise HTTPException(
            status_code=422, detail=f"At least one of {', '.join(required_any)} is required"
        )

    async def apply_extras(db, user, obj, tags, cfs):
        if tags is not None:
            await set_tags(db, user.org_id, entity_type, obj.id, tags)
        if cfs is not None:
            await set_custom_fields(db, user.org_id, entity_type, obj.id, cfs)

    @router.get("")
    async def list_items(
        request: Request,
        q: str | None = None,
        page: int = 1,
        page_size: int = 50,
        sort: str | None = None,
        order: str = "asc",
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        page = max(1, page)
        page_size = min(max(1, page_size), 200)
        stmt = base_query(user)

        if q and search_cols:
            pattern = f"%{q.strip().lower()}%"
            stmt = stmt.where(
                or_(*[func.lower(func.coalesce(c, "")).like(pattern) for c in search_cols])
            )

        for name, col in filterable.items():
            raw = request.query_params.get(name)
            if raw in (None, ""):
                continue
            try:
                pytype = col.type.python_type
            except NotImplementedError:
                pytype = str
            val = raw
            if pytype is uuid.UUID:
                try:
                    val = uuid.UUID(raw)
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"Invalid {name}")
            stmt = stmt.where(col == val)

        tag_name = request.query_params.get("tag")
        if tag_name and has_extras:
            stmt = stmt.where(
                model.id.in_(
                    select(EntityTag.entity_id)
                    .join(Tag, Tag.id == EntityTag.tag_id)
                    .where(
                        EntityTag.entity_type == entity_type,
                        Tag.org_id == user.org_id,
                        Tag.name == tag_name,
                    )
                )
            )

        total = (
            await db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()

        sort_col = sortable.get(sort or default_sort) or sortable[default_sort]
        stmt = stmt.order_by(sort_col.desc() if order == "desc" else sort_col.asc())
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        items = (await db.execute(stmt)).scalars().all()

        return {
            "items": await serialize(db, user, list(items)),
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @router.post("", status_code=201)
    async def create_item(
        body: body_model,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        data = body.model_dump(exclude_unset=True)
        tags = data.pop("tags", None)
        cfs = data.pop("custom_fields", None)
        check_required(data)
        obj = model(org_id=user.org_id, **data)
        if hasattr(obj, "created_by") and obj.created_by is None:
            obj.created_by = user.id
        if hasattr(obj, "owner_id") and obj.owner_id is None:
            obj.owner_id = user.id
        db.add(obj)
        await db.flush()
        await apply_extras(db, user, obj, tags, cfs)
        await log_activity(db, user.org_id, entity_type, obj.id, "created", user.id)
        if after_create is not None:
            after_create(obj)
        return (await serialize(db, user, [obj]))[0]

    @router.get("/{item_id}")
    async def get_item(
        item_id: uuid.UUID,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        obj = await get_owned(db, user, item_id)
        return (await serialize(db, user, [obj]))[0]

    @router.patch("/{item_id}")
    async def update_item(
        item_id: uuid.UUID,
        body: body_model,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        obj = await get_owned(db, user, item_id)
        data = body.model_dump(exclude_unset=True)
        tags = data.pop("tags", None)
        cfs = data.pop("custom_fields", None)
        for key, value in data.items():
            setattr(obj, key, value)
        if hasattr(obj, "updated_at"):
            obj.updated_at = utcnow()
        await apply_extras(db, user, obj, tags, cfs)
        if data or tags is not None or cfs is not None:
            await log_activity(
                db, user.org_id, entity_type, obj.id, "updated", user.id,
                {"fields": sorted(data.keys())},
            )
        await db.flush()
        return (await serialize(db, user, [obj]))[0]

    @router.delete("/{item_id}", status_code=204)
    async def delete_item(
        item_id: uuid.UUID,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        obj = await get_owned(db, user, item_id)
        label = getattr(obj, "name", None) or " ".join(
            filter(None, [getattr(obj, "first_name", None), getattr(obj, "last_name", None)])
        )
        await cleanup_entity(db, entity_type, obj.id)
        await db.delete(obj)
        await log_activity(
            db, user.org_id, None, None, f"{entity_type}_deleted", user.id, {"label": label}
        )
