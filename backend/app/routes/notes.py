import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import Note, PhoneEvent, User, utcnow
from app.schemas import NoteAttachIn, NoteIn, NoteListOut, NoteOut
from app.services import contacts, entities, timeline
from app.services.common import (
    validate_entity_ref,
)
from app.services.contacts import people_with_number
from app.services.interactions import update_person_aggregates


async def _event_on_note_record(db, org_id, note: Note, event_id: uuid.UUID) -> None:
    """A note may only hang on a call/text that belongs on the SAME record's
    timeline — the rule the phone-events listing uses: logged directly on the
    record, or matched to one of its numbers (for a company: its people's
    numbers, and events logged on those people). Linking to anything else
    would pull the note off its own record's timeline."""
    ev = (
        await db.execute(
            select(PhoneEvent).where(PhoneEvent.id == event_id, PhoneEvent.org_id == org_id)
        )
    ).scalar_one_or_none()
    if ev is None:
        raise HTTPException(status_code=404, detail="Call or text not found")
    mismatch = HTTPException(
        status_code=422, detail="That call or text belongs to a different record"
    )
    try:
        scope = await timeline.record_scope(db, org_id, note.entity_type, note.entity_id)
    except KeyError:
        scope = None
    # Opportunities and the like have no call timeline (scope.points is None).
    if scope is None or not timeline.phone_event_on_record(ev, scope):
        raise mismatch


router = APIRouter()


async def _serialize(db, org_id, notes: list[Note]) -> list[dict]:
    return await timeline.note_rows(db, org_id, notes)


@router.get("", response_model=NoteListOut)
async def list_notes(
    entity_type: str,
    entity_id: uuid.UUID,
    limit: int = 500,
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
                # A record with years of notes must not come back unbounded.
                .limit(max(1, min(limit, 2000)))
            )
        )
        .scalars()
        .all()
    )
    return {"items": await _serialize(db, user.org_id, list(notes))}


@router.post("", status_code=201, response_model=NoteOut)
async def create_note(
    body: NoteIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    if not body.body.strip():
        raise HTTPException(status_code=422, detail="Note body is required")
    # The note's target must be a real in-org record — don't let a caller
    # attach a note to another org's row (or a bogus id). The log_call branch
    # below re-checks person/lead specifically; this covers every entity type.
    await validate_entity_ref(db, user.org_id, body.entity_type, body.entity_id)
    if body.phone_event_id is not None:
        # Same rule as relinking: the call/text must sit on this record's
        # own timeline, or the new note would vanish from it.
        await _event_on_note_record(
            db,
            user.org_id,
            Note(entity_type=body.entity_type, entity_id=body.entity_id),
            body.phone_event_id,
        )

    phone_event_id = body.phone_event_id
    if body.log_call and phone_event_id is None:
        # Not every call goes through RingCentral — log one manually and hang
        # the note on it, so it counts and displays like any synced call.
        # A call is logged against a number, so only on a record type that
        # has numbers of its own.
        if body.entity_type not in contacts.PHONE_FIELDS:
            raise HTTPException(status_code=422, detail="Calls can be logged on people and leads")
        record = await entities.get_record(db, user.org_id, body.entity_type, body.entity_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"{body.entity_type} not found")
        number = next(iter(contacts.record_numbers(body.entity_type, record)), "")
        direction = body.call_direction if body.call_direction in ("inbound", "outbound") else "outbound"
        event = PhoneEvent(
            org_id=user.org_id,
            rc_id=f"manual:{uuid.uuid4().hex}",
            kind="call",
            direction=direction,
            other_number=number,
            happened_at=utcnow(),
            entity_type=body.entity_type,
            entity_id=body.entity_id,
        )
        db.add(event)
        await db.flush()
        phone_event_id = event.id
        # The logged call counts as an interaction right away — for the
        # person it was logged on and for anyone else listing that number
        # (events are matched by number, so it shows on all of them).
        affected = await people_with_number(db, user.org_id, number)
        if body.entity_type == "person":
            affected.add(body.entity_id)
        await update_person_aggregates(db, user.org_id, affected)

    note = Note(
        org_id=user.org_id,
        entity_type=body.entity_type,
        entity_id=body.entity_id,
        body=body.body,
        phone_event_id=phone_event_id,
        author_id=user.id,
    )
    db.add(note)
    await db.flush()
    result = (await _serialize(db, user.org_id, [note]))[0]
    await db.commit()  # visible before the client refetches
    return result


@router.patch("/{note_id}", response_model=NoteOut)
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
    # Same rule as delete: relinking changes where (and whether) the note
    # shows on the timeline, so it is the author's call, or an admin's.
    if note.author_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="Only the author or an admin can relink a note")
    if body.phone_event_id is not None:
        await _event_on_note_record(db, user.org_id, note, body.phone_event_id)
    note.phone_event_id = body.phone_event_id
    result = (await _serialize(db, user.org_id, [note]))[0]
    await db.commit()  # visible before the client refetches
    return result


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
    orphan_event_id = note.phone_event_id
    await db.delete(note)
    if orphan_event_id is not None:
        await db.flush()
        event = (
            await db.execute(
                select(PhoneEvent).where(
                    PhoneEvent.id == orphan_event_id, PhoneEvent.org_id == user.org_id
                )
            )
        ).scalar_one_or_none()
        if event is not None and event.rc_id.startswith("manual:"):
            still_referenced = (
                await db.execute(select(Note.id).where(Note.phone_event_id == event.id).limit(1))
            ).scalar_one_or_none()
            if still_referenced is None:
                await db.delete(event)
                # The call is gone — so is its place in the interaction count
                # and last-contacted date of everyone it counted for.
                await db.flush()
                affected = await people_with_number(db, user.org_id, event.other_number)
                if event.entity_type == "person" and event.entity_id is not None:
                    affected.add(event.entity_id)
                await update_person_aggregates(db, user.org_id, affected)
    await db.commit()  # visible before the client refetches
