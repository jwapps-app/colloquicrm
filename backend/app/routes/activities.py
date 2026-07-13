import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import Activity, User
from app.services.common import display_name_map, row_to_dict

router = APIRouter()


@router.get("")
async def list_activities(
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    page = max(1, page)
    page_size = min(max(1, page_size), 200)
    stmt = select(Activity).where(Activity.org_id == user.org_id)
    if entity_type:
        stmt = stmt.where(Activity.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(Activity.entity_id == entity_id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = (
        stmt.order_by(Activity.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = (await db.execute(stmt)).scalars().all()
    names = await display_name_map(db, {a.actor_id for a in items}, user.org_id)
    dicts = []
    for a in items:
        d = row_to_dict(a)
        d["actor_name"] = names.get(d.get("actor_id"))
        dicts.append(d)
    return {"items": dicts, "total": total, "page": page, "page_size": page_size}
