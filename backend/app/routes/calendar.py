import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import CalendarEvent, CalendarEventAttendee, User
from app.schemas import CalendarEventListOut
from app.services import timeline

router = APIRouter()


@router.get("", response_model=CalendarEventListOut)
async def calendar_events(
    entity_type: str,
    entity_id: uuid.UUID,
    limit: int = 25,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Synced calendar events a person, lead or company is an attendee of."""
    scope = await timeline.require_scope(
        db, user.org_id, entity_type, entity_id, correspondents_only=True
    )
    points = scope.points
    stmt = (
        select(CalendarEvent)
        .join(CalendarEventAttendee, CalendarEventAttendee.event_id == CalendarEvent.id)
        .where(CalendarEvent.org_id == user.org_id)
        .distinct()
    )
    if entity_type == "company":
        # A company's meetings are matched by its email domain only.
        if not points.domain:
            return {"items": []}
        stmt = stmt.where(CalendarEventAttendee.email.like(f"%@{points.domain}"))
    else:
        if not points.emails:
            return {"items": []}
        stmt = stmt.where(CalendarEventAttendee.email.in_(points.emails))

    stmt = stmt.order_by(CalendarEvent.starts_at.desc()).limit(min(max(limit, 1), 100))
    events = (await db.execute(stmt)).scalars().all()
    ids = [e.id for e in events]
    attendee_map: dict = {}
    if ids:
        rows = await db.execute(
            select(CalendarEventAttendee).where(CalendarEventAttendee.event_id.in_(ids))
        )
        for att in rows.scalars():
            attendee_map.setdefault(att.event_id, []).append(
                {"email": att.email, "display_name": att.display_name}
            )
    return {
        "items": [
            {
                "id": str(e.id),
                "summary": e.summary,
                "location": e.location,
                "starts_at": e.starts_at.isoformat() if e.starts_at else None,
                "ends_at": e.ends_at.isoformat() if e.ends_at else None,
                "all_day": e.all_day,
                "html_link": e.html_link,
                "attendees": attendee_map.get(e.id, []),
            }
            for e in events
        ]
    }
