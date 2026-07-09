import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import ContactSuggestion, Person, User, utcnow
from app.services.common import log_activity

router = APIRouter()


def _split_name(display_name: str | None, email: str) -> tuple[str, str]:
    name = (display_name or "").strip()
    if not name:
        return email.split("@", 1)[0], ""
    parts = name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


@router.get("")
async def list_suggestions(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    rows = (
        (
            await db.execute(
                select(ContactSuggestion)
                .where(
                    ContactSuggestion.org_id == user.org_id,
                    ContactSuggestion.status == "pending",
                )
                .order_by(
                    ContactSuggestion.message_count.desc(),
                    ContactSuggestion.last_seen_at.desc().nulls_last(),
                )
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(s.id),
                "email": s.email,
                "display_name": s.display_name,
                "message_count": s.message_count,
                "last_seen_at": s.last_seen_at.isoformat() if s.last_seen_at else None,
            }
            for s in rows
        ]
    }


async def _get_pending(db, user, sug_id):
    s = (
        await db.execute(
            select(ContactSuggestion).where(
                ContactSuggestion.id == sug_id, ContactSuggestion.org_id == user.org_id
            )
        )
    ).scalar_one_or_none()
    if s is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    return s


@router.post("/{sug_id}/add")
async def add_suggestion(
    sug_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Turn a suggestion into a real Person and mark it handled."""
    s = await _get_pending(db, user, sug_id)
    first, last = _split_name(s.display_name, s.email)
    person = Person(
        org_id=user.org_id,
        first_name=first,
        last_name=last or None,
        work_email=s.email,
        owner_id=user.id,
        contact_type="Uncategorized",
    )
    db.add(person)
    s.status = "added"
    s.updated_at = utcnow()
    await db.flush()
    await log_activity(db, user.org_id, "person", person.id, "created", user.id)
    result = {"person_id": str(person.id), "first_name": first, "last_name": last}
    await db.commit()  # visible before the client refetches
    return result


@router.post("/{sug_id}/ignore", status_code=204)
async def ignore_suggestion(
    sug_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Dismiss a suggestion for good — it won't be surfaced again."""
    s = await _get_pending(db, user, sug_id)
    s.status = "ignored"
    s.updated_at = utcnow()
    await db.commit()
