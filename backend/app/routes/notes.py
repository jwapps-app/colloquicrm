import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import Note, PhoneEvent, User
from app.schemas import NoteAttachIn, NoteIn
from app.services.common import display_name_map, row_to_dict


async def _valid_phone_event(db, org_id, phone_event_id):
    if phone_event_id is None:
        return
    ev = (
        await db.execute(
            select(PhoneEvent).where(
                PhoneEvent.id == phone_event_id, PhoneEvent.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if ev is None:
        raise HTTPException(status_code=404, detail="Call or text not found")

router = APIRouter()


async def _serialize(db, notes: list[Note]) -> list[dict]:
    names = await display_name_map(db, {n.author_id for n in notes})
    out = []
    for n in notes:
        d = row_to_dict(n)
        d["author_name"] = names.get(d.get("author_id"))
        out.append(d)
    return out


@router.get("")
async def list_notes(
    entity_type: str,
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    notes = (
        (
            await db.execute(
                select(Note)
                .where(
                    Note.org_id == user.org_id,
                    Note.entity_type == entity_type,
                    Note.entity_id == entity_id,
                )
                .order_by(Note.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return {"items": await _serialize(db, list(notes))}


@router.post("", status_code=201)
async def create_note(
    body: NoteIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    if not body.body.strip():
        raise HTTPException(status_code=422, detail="Note body is required")
    await _valid_phone_event(db, user.org_id, body.phone_event_id)
    note = Note(
        org_id=user.org_id,
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        body=body.body,
        phone_event_id=body.phone_event_id,
        author_id=user.id,
    )
    db.add(note)
    await db.flush()
    return (await _serialize(db, [note]))[0]


@router.patch("/{note_id}")
async def attach_note(
    note_id: uuid.UUID,
    body: NoteAttachIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Link (or unlink) a note to a logged call/text — the 'merge after the
    fact' path for notes jotted during a call."""
    note = (
        await db.execute(select(Note).where(Note.id == note_id, Note.org_id == user.org_id))
    ).scalar_one_or_none()
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    await _valid_phone_event(db, user.org_id, body.phone_event_id)
    note.phone_event_id = body.phone_event_id
    return (await _serialize(db, [note]))[0]


@router.delete("/{note_id}", status_code=204)
async def delete_note(
    note_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    note = (
        await db.execute(select(Note).where(Note.id == note_id, Note.org_id == user.org_id))
    ).scalar_one_or_none()
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    if note.author_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="Only the author or an admin can delete a note")
    await db.delete(note)
