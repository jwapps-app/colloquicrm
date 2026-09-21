import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import Company, Lead, Note, Person, PhoneEvent, User, utcnow
from app.schemas import NoteAttachIn, NoteIn
from app.services.common import (
    company_people,
    display_name_map,
    row_to_dict,
    validate_entity_ref,
)
from app.services.interactions import people_with_number, update_person_aggregates
from app.services.ringcentral import normalize_phone


_PHONE_TIMELINE_MODELS = {"person": Person, "lead": Lead, "company": Company}


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
    model = _PHONE_TIMELINE_MODELS.get(note.entity_type)
    if model is None:
        raise mismatch  # opportunities and the like have no call timeline
    if ev.entity_type == note.entity_type and ev.entity_id == note.entity_id:
        return
    if note.entity_type == "company":
        holders = list(await company_people(db, org_id, note.entity_id))
        if ev.entity_type == "person" and ev.entity_id in {p.id for p in holders}:
            return
    else:
        record = (
            await db.execute(
                select(model).where(model.id == note.entity_id, model.org_id == org_id)
            )
        ).scalar_one_or_none()
        holders = [record] if record is not None else []
    numbers = {
        n
        for h in holders
        for n in (normalize_phone(h.work_phone), normalize_phone(h.mobile_phone))
        if n
    }
    if not ev.other_number or ev.other_number not in numbers:
        raise mismatch


router = APIRouter()


async def _serialize(db, org_id, notes: list[Note]) -> list[dict]:
    names = await display_name_map(db, {n.author_id for n in notes}, org_id)
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


@router.post("", status_code=201)
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
        if body.entity_type not in ("person", "lead"):
            raise HTTPException(status_code=422, detail="Calls can be logged on people and leads")
        model = Person if body.entity_type == "person" else Lead
        record = (
            await db.execute(
                select(model).where(model.id == body.entity_id, model.org_id == user.org_id)
            )
        ).scalar_one_or_none()
        if record is None:
            raise HTTPException(status_code=404, detail=f"{body.entity_type} not found")
        number = normalize_phone(record.work_phone) or normalize_phone(record.mobile_phone) or ""
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
