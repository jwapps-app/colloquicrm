import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import ContactSuggestion, Person, User, utcnow
from app.services.common import log_activity
from app.services.interactions import update_person_aggregates

router = APIRouter()


class AddSuggestionIn(BaseModel):
    contact_type: str | None = None


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


async def _claim_pending(db, user, sug_id, new_status: str) -> ContactSuggestion:
    """Move a suggestion out of 'pending' atomically and return it.

    Check-and-set in one UPDATE: a double-click (or two people working the
    same list) used to both read 'pending' and both mint a Person. Now only
    one request's UPDATE matches; the other waits on the row lock, re-checks
    the WHERE once the first commits, and gets the 409."""
    claimed = (
        await db.execute(
            update(ContactSuggestion)
            .where(
                ContactSuggestion.id == sug_id,
                ContactSuggestion.org_id == user.org_id,
                ContactSuggestion.status == "pending",
            )
            .values(status=new_status, updated_at=utcnow())
            .returning(ContactSuggestion.id)
        )
    ).scalar_one_or_none()
    s = (
        await db.execute(
            select(ContactSuggestion).where(
                ContactSuggestion.id == sug_id, ContactSuggestion.org_id == user.org_id
            )
        )
    ).scalar_one_or_none()
    if s is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    if claimed is None:
        # A double-click (or stale list) must not mint a second Person or
        # silently flip an earlier decision.
        raise HTTPException(
            status_code=409, detail=f"Suggestion was already {s.status}"
        )
    return s


@router.post("/{sug_id}/add")
async def add_suggestion(
    sug_id: uuid.UUID,
    body: AddSuggestionIn | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Turn a suggestion into a real Person and mark it handled."""
    s = await _claim_pending(db, user, sug_id, "added")
    first, last = _split_name(s.display_name, s.email)
    contact_type = (body.contact_type if body and body.contact_type else "Uncategorized")
    person = Person(
        org_id=user.org_id,
        first_name=first,
        last_name=last or None,
        work_email=s.email,
        owner_id=user.id,
        contact_type=contact_type,
    )
    db.add(person)
    await db.flush()
    # The suggestion exists because this address is all over the mailbox —
    # and whatever of that mail is archived counts from the start.
    await update_person_aggregates(db, user.org_id, {person.id})
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
    await _claim_pending(db, user, sug_id, "ignored")
    await db.commit()
