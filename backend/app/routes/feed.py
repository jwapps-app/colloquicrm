import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import User
from app.schemas import TimelinePageOut
from app.services import timeline

router = APIRouter()
timeline_router = APIRouter()


@router.get("", response_model=TimelinePageOut, response_model_exclude_unset=True)
async def feed(
    page: int = 1,
    page_size: int = 30,
    kind: str = "all",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Everything happening across the org, newest first."""
    return await timeline.timeline_page(
        db, user.org_id, kind=kind, page=page, page_size=page_size
    )


@timeline_router.get("", response_model=TimelinePageOut, response_model_exclude_unset=True)
async def record_timeline(
    entity_type: str,
    entity_id: uuid.UUID,
    page: int = 1,
    page_size: int = 50,
    kind: str = "all",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """One record's history — its notes and activity, plus the emails, calls
    and texts that belong to it — in the feed's shape and order. A call or
    text carries this record's notes about it in `notes`."""
    scope = await timeline.require_scope(db, user.org_id, entity_type, entity_id)
    return await timeline.timeline_page(
        db, user.org_id, kind=kind, page=page, page_size=page_size, scope=scope
    )

