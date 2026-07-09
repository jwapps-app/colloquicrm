import csv
import io
import uuid
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import Select, delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import (
    Activity,
    CustomField,
    CustomFieldValue,
    EntityTag,
    Note,
    PhoneEvent,
    Tag,
    Task,
    User,
    utcnow,
)
from app.services.common import (
    add_tags,
    cleanup_entity,
    get_cf_maps,
    get_or_create_tag,
    get_tag_maps,
    log_activity,
    row_to_dict,
    set_custom_fields,
    set_tags,
)

Enricher = Callable[[AsyncSession, User, list[dict]], Awaitable[None]]

EXPORT_MAX_ROWS = 100_000
BATCH = 500

PLURALS = {"person": "people", "company": "companies", "opportunity": "opportunities"}


class BulkIn(BaseModel):
    action: str  # delete | add_tags | set_owner
    ids: list[uuid.UUID] = []
    select_all: bool = False  # act on everything matching the query filters
    tags: list[str] | None = None
    owner_id: uuid.UUID | None = None


class MergeIn(BaseModel):
    source_id: uuid.UUID


def _chunks(seq: list, size: int = BATCH):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


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
    merge_refs: list[tuple] | None = None,  # (Model, fk attr name) to re-point on merge
    after_merge: Callable | None = None,
    extra_filter: Callable | None = None,  # (request, user, stmt) -> stmt
) -> None:
    """Wires list/create/get/patch/delete endpoints for one entity onto a
    router. body_model serves both create and PATCH (exclude_unset)."""

    soft_delete = hasattr(model, "deleted_at")

    def _active(stmt: Select) -> Select:
        # Trashed records are invisible everywhere except the trash view.
        return stmt.where(model.deleted_at.is_(None)) if soft_delete else stmt

    def base_query(user: User) -> Select:
        return _active(select(model).where(model.org_id == user.org_id))

    async def get_owned(db: AsyncSession, user: User, item_id: uuid.UUID):
        obj = (
            await db.execute(
                _active(select(model).where(model.id == item_id, model.org_id == user.org_id))
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

    def filtered_stmt(request: Request, user: User, q: str | None) -> Select:
        """The list query minus sort/pagination — shared by list, export, and
        select-all bulk actions so 'what you see' and 'what you act on' match."""
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
        if extra_filter is not None:
            stmt = extra_filter(request, user, stmt)
        return stmt

    def sort_clause(sort: str | None, order: str):
        sort_col = sortable.get(sort or default_sort) or sortable[default_sort]
        clause = sort_col.desc() if order == "desc" else sort_col.asc()
        # Records without a value ("never contacted", no close date) belong at
        # the bottom whichever way you sort — Postgres defaults NULLs to the
        # top on DESC.
        return clause.nulls_last()

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
        stmt = filtered_stmt(request, user, q)

        total = (
            await db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()

        stmt = stmt.order_by(sort_clause(sort, order))
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        items = (await db.execute(stmt)).scalars().all()

        return {
            "items": await serialize(db, user, list(items)),
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @router.get("/export")
    async def export_csv(
        request: Request,
        q: str | None = None,
        sort: str | None = None,
        order: str = "asc",
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        """Everything matching the current filters, as CSV: standard fields,
        display names, tags, and one column per custom field."""
        stmt = filtered_stmt(request, user, q).order_by(sort_clause(sort, order))
        total = (
            await db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        if total > EXPORT_MAX_ROWS:
            raise HTTPException(
                status_code=413,
                detail=f"Export too large ({total} rows; {EXPORT_MAX_ROWS} max). Narrow the filters.",
            )
        items = (await db.execute(stmt)).scalars().all()

        dicts: list[dict] = []
        for chunk in _chunks(list(items)):
            dicts.extend(await serialize(db, user, chunk))

        base_cols = [
            attr for attr in dicts[0].keys() if attr not in ("tags", "custom_fields")
        ] if dicts else []
        cf_defs = []
        if has_extras:
            cf_defs = (
                await db.execute(
                    select(CustomField)
                    .where(CustomField.org_id == user.org_id, CustomField.entity_type == entity_type)
                    .order_by(CustomField.position)
                )
            ).scalars().all()

        def sanitize(value):
            # Spreadsheet formula injection: a leading =, +, -, or @ makes
            # Excel/Sheets execute the cell. Imported data can plant these.
            if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
                return "'" + value
            return value

        buf = io.StringIO()
        writer = csv.writer(buf)
        header = base_cols + (["tags"] if has_extras else []) + [d.name for d in cf_defs]
        writer.writerow(header)
        for d in dicts:
            row = [sanitize(d.get(c)) if d.get(c) is not None else "" for c in base_cols]
            if has_extras:
                row.append(sanitize("; ".join(d.get("tags") or [])))
            cf_values = d.get("custom_fields") or {}
            row.extend(sanitize(cf_values.get(str(cd.id)) or "") for cd in cf_defs)
            writer.writerow(row)

        plural = PLURALS.get(entity_type, f"{entity_type}s")
        filename = f"{plural}-{utcnow().date().isoformat()}.csv"
        # BOM so Excel opens it as UTF-8.
        return Response(
            content="\ufeff" + buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.post("/bulk")
    async def bulk_action(
        request: Request,
        body: BulkIn,
        q: str | None = None,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        """Act on many records at once. Targets are explicit ids, or with
        select_all=true everything matching the query-string filters."""
        if body.action not in ("delete", "add_tags", "set_owner"):
            raise HTTPException(status_code=422, detail="Unknown bulk action")

        if body.select_all:
            stmt = filtered_stmt(request, user, q).with_only_columns(model.id)
            ids = [rid for (rid,) in await db.execute(stmt)]
        else:
            ids = list(dict.fromkeys(body.ids))
            if ids:
                owned = set()
                for chunk in _chunks(ids):
                    rows = await db.execute(
                        select(model.id).where(model.org_id == user.org_id, model.id.in_(chunk))
                    )
                    owned.update(rid for (rid,) in rows)
                ids = [i for i in ids if i in owned]
        if not ids:
            return {"affected": 0}

        if body.action == "delete":
            for chunk in _chunks(ids):
                if soft_delete:
                    # To Trash in bulk — satellites stay attached for restore.
                    await db.execute(
                        update(model)
                        .where(model.org_id == user.org_id, model.id.in_(chunk))
                        .values(deleted_at=utcnow())
                    )
                    continue
                for sat, type_col, id_col in (
                    (EntityTag, EntityTag.entity_type, EntityTag.entity_id),
                    (CustomFieldValue, CustomFieldValue.entity_type, CustomFieldValue.entity_id),
                    (Note, Note.entity_type, Note.entity_id),
                ):
                    await db.execute(
                        delete(sat).where(type_col == entity_type, id_col.in_(chunk))
                    )
                await db.execute(
                    delete(model).where(model.org_id == user.org_id, model.id.in_(chunk))
                )
            await log_activity(
                db, user.org_id, None, None, f"{entity_type}_bulk_deleted", user.id,
                {"count": len(ids)},
            )
        elif body.action == "add_tags":
            names = [n.strip() for n in (body.tags or []) if n and n.strip()]
            if not names:
                raise HTTPException(status_code=422, detail="No tags given")
            # Set-based: resolve tags once, then per chunk one existence check
            # and one bulk insert — not two queries per record.
            tag_ids = [(await get_or_create_tag(db, user.org_id, n)).id for n in names]
            await db.flush()
            for chunk in _chunks(ids):
                existing = {
                    (tid, rid)
                    for tid, rid in await db.execute(
                        select(EntityTag.tag_id, EntityTag.entity_id).where(
                            EntityTag.entity_type == entity_type,
                            EntityTag.tag_id.in_(tag_ids),
                            EntityTag.entity_id.in_(chunk),
                        )
                    )
                }
                rows = [
                    {"tag_id": tid, "entity_type": entity_type, "entity_id": rid}
                    for rid in chunk
                    for tid in tag_ids
                    if (tid, rid) not in existing
                ]
                if rows:
                    await db.execute(insert(EntityTag), rows)
        elif body.action == "set_owner":
            if not hasattr(model, "owner_id"):
                raise HTTPException(status_code=422, detail="Records have no owner")
            owner = (
                await db.execute(
                    select(User).where(User.id == body.owner_id, User.org_id == user.org_id)
                )
            ).scalar_one_or_none()
            if owner is None:
                raise HTTPException(status_code=404, detail="Owner not found")
            values = {"owner_id": owner.id}
            if hasattr(model, "updated_at"):
                values["updated_at"] = utcnow()
            for chunk in _chunks(ids):
                await db.execute(
                    update(model)
                    .where(model.org_id == user.org_id, model.id.in_(chunk))
                    .values(**values)
                )
        await db.commit()
        return {"affected": len(ids)}

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
        result = (await serialize(db, user, [obj]))[0]
        # The client acts on the response immediately (navigate, refetch); the
        # framework's commit lands after the response, which loses that race
        # on a slow-commit database. Commit before returning.
        await db.commit()
        return result

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
        result = (await serialize(db, user, [obj]))[0]
        await db.commit()
        return result

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
        if soft_delete:
            # To Trash — recoverable for the retention window. Satellites stay
            # attached so a restore brings the record back whole.
            obj.deleted_at = utcnow()
        else:
            await cleanup_entity(db, entity_type, obj.id)
            await db.delete(obj)
        await log_activity(
            db, user.org_id, None, None, f"{entity_type}_deleted", user.id, {"label": label}
        )
        await db.commit()

    if soft_delete:

        @router.get("/trash/list")
        async def list_trash(
            user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
        ):
            rows = (
                (
                    await db.execute(
                        select(model)
                        .where(model.org_id == user.org_id, model.deleted_at.is_not(None))
                        .order_by(model.deleted_at.desc())
                        .limit(500)
                    )
                )
                .scalars()
                .all()
            )
            out = []
            for r in rows:
                label = getattr(r, "name", None) or " ".join(
                    filter(None, [getattr(r, "first_name", None), getattr(r, "last_name", None)])
                )
                out.append(
                    {
                        "id": str(r.id),
                        "label": label or "(unnamed)",
                        "deleted_at": r.deleted_at.isoformat() if r.deleted_at else None,
                    }
                )
            return {"items": out, "entity_type": entity_type}

        @router.post("/{item_id}/restore")
        async def restore_item(
            item_id: uuid.UUID,
            user: User = Depends(get_current_user),
            db: AsyncSession = Depends(get_db),
        ):
            obj = (
                await db.execute(
                    select(model).where(
                        model.id == item_id,
                        model.org_id == user.org_id,
                        model.deleted_at.is_not(None),
                    )
                )
            ).scalar_one_or_none()
            if obj is None:
                raise HTTPException(status_code=404, detail="Not in trash")
            obj.deleted_at = None
            if hasattr(obj, "updated_at"):
                obj.updated_at = utcnow()
            await log_activity(db, user.org_id, entity_type, obj.id, "restored", user.id)
            await db.commit()
            return {"id": str(obj.id), "restored": True}

    MERGE_SKIP = {"id", "org_id", "created_at", "updated_at", "interaction_count",
                  "last_contacted_at"}

    @router.post("/{item_id}/merge")
    async def merge_item(
        item_id: uuid.UUID,
        body: MergeIn,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        """Fold a duplicate into this record: empty fields fill from the
        duplicate; notes, tasks, activity, calls, tags, and custom values move
        over; references re-point; the duplicate is deleted."""
        if body.source_id == item_id:
            raise HTTPException(status_code=422, detail="A record can't merge with itself")
        target = await get_owned(db, user, item_id)
        source = await get_owned(db, user, body.source_id)

        source_label = getattr(source, "name", None) or " ".join(
            filter(None, [getattr(source, "first_name", None), getattr(source, "last_name", None)])
        )

        for col in model.__table__.columns:
            key = col.key
            if key in MERGE_SKIP:
                continue
            if getattr(target, key) in (None, "") and getattr(source, key) not in (None, ""):
                setattr(target, key, getattr(source, key))

        # Satellites keyed by (entity_type, entity_id) follow the survivor.
        for sat_model in (Note, Task, Activity, PhoneEvent):
            await db.execute(
                update(sat_model)
                .where(
                    sat_model.entity_type == entity_type,
                    sat_model.entity_id == source.id,
                )
                .values(entity_id=target.id)
            )

        if has_extras:
            source_tags = [
                name
                for (name,) in await db.execute(
                    select(Tag.name)
                    .join(EntityTag, EntityTag.tag_id == Tag.id)
                    .where(EntityTag.entity_type == entity_type, EntityTag.entity_id == source.id)
                )
            ]
            if source_tags:
                await add_tags(db, user.org_id, entity_type, target.id, source_tags)

            taken = {
                fid
                for (fid,) in await db.execute(
                    select(CustomFieldValue.field_id).where(
                        CustomFieldValue.entity_type == entity_type,
                        CustomFieldValue.entity_id == target.id,
                    )
                )
            }
            source_cfs = (
                await db.execute(
                    select(CustomFieldValue).where(
                        CustomFieldValue.entity_type == entity_type,
                        CustomFieldValue.entity_id == source.id,
                    )
                )
            ).scalars().all()
            for cfv in source_cfs:
                if cfv.field_id not in taken:
                    await db.execute(
                        update(CustomFieldValue)
                        .where(
                            CustomFieldValue.field_id == cfv.field_id,
                            CustomFieldValue.entity_id == source.id,
                        )
                        .values(entity_id=target.id)
                    )

        for ref_model, attr in merge_refs or []:
            await db.execute(
                update(ref_model)
                .where(ref_model.org_id == user.org_id, getattr(ref_model, attr) == source.id)
                .values(**{attr: target.id})
            )

        if soft_delete:
            # The merged record goes to Trash (its data already moved to the
            # survivor), so even a mistaken merge is recoverable.
            source.deleted_at = utcnow()
        else:
            await cleanup_entity(db, entity_type, source.id)
            await db.delete(source)
        if hasattr(target, "updated_at"):
            target.updated_at = utcnow()
        await log_activity(
            db, user.org_id, entity_type, target.id, "merged", user.id,
            {"source_label": source_label, "source_id": str(body.source_id)},
        )
        await db.flush()
        if after_merge is not None:
            await after_merge(db, user, target)
        result = (await serialize(db, user, [target]))[0]
        await db.commit()
        return result
