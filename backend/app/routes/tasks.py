import calendar
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import as_utc, get_current_user
from app.models import Task, User, utcnow
from app.schemas import MAX_TASK_DATE, TaskIn
from app.services import colloqui
from app.services.background import after_commit
from app.services.common import (
    display_name_map,
    entity_labels_map,
    log_activity,
    validate_entity_ref,
)
from app.services.crud import register_crud

router = APIRouter()


async def enrich(db, user, dicts):
    names = await display_name_map(
        db,
        {d.get("assignee_id") for d in dicts} | {d.get("created_by") for d in dicts},
        user.org_id,
    )
    labels = await entity_labels_map(
        db,
        user.org_id,
        {(d.get("entity_type"), d.get("entity_id")) for d in dicts},
    )
    for d in dicts:
        d["assignee_name"] = names.get(d.get("assignee_id"))
        d["created_by_name"] = names.get(d.get("created_by"))
        # The record this task hangs off, by name — clients show "Related to
        # <name>" and fall back to the type when the record is gone (null).
        d["entity_label"] = labels.get((d.get("entity_type"), d.get("entity_id")))


async def _validate_target(db, user, data, current):
    # A task can hang off a person/company/opportunity/lead — make sure the
    # target is a real in-org record before we store the pointer. A PATCH may
    # send just one half of the pair; the other half is whatever the task
    # already has, and the combination is what gets validated — otherwise a
    # lone entity_id could re-point a task at a record in another org (or at
    # nothing). `current` is None on create, where the body is the whole story.
    if "entity_type" not in data and "entity_id" not in data:
        return
    entity_type = (
        data.get("entity_type")
        if "entity_type" in data
        else getattr(current, "entity_type", None)
    )
    entity_id = (
        data.get("entity_id") if "entity_id" in data else getattr(current, "entity_id", None)
    )
    await validate_entity_ref(db, user.org_id, entity_type, entity_id)


def _notify_later(db, task_id, event, actor_id, assignee_id) -> None:
    """Queue a task notification for AFTER the commit. The notifier re-reads
    the task from its own session, so it must not start until the write is
    durable — scheduling it mid-transaction raced the commit (and announced
    tasks whose transaction then rolled back). Plain ids are captured here;
    the ORM objects belong to a transaction that will be over by then."""
    after_commit(
        db,
        lambda: colloqui.schedule(
            colloqui.notify_task_event(
                task_id, event, actor_id=actor_id, assignee_id=assignee_id
            )
        ),
    )


def _notify_created(db, task, actor):
    # Only a genuine hand-off notifies. A task you create for yourself (or leave
    # unassigned) needs no "you were assigned" ping and no team #tasks post —
    # only assigning it to someone else does.
    if task.assignee_id and task.assignee_id != actor.id:
        _notify_later(db, task.id, "created", actor.id, task.assignee_id)


def _after_update(db, task, old_values, actor):
    # Re-arm the reminder when the schedule moves: the sweep only notifies
    # tasks with due_notified_at unset, so a fired reminder would otherwise
    # never fire again for the new due/reminder time. Runs pre-commit, so the
    # clear lands in the same transaction as the PATCH.
    for key in ("due_at", "reminder_at"):
        if key in old_values and getattr(task, key) != old_values[key]:
            task.due_notified_at = None
            break
    # Reassignment (not initial creation — that's the "created" event) to
    # someone other than the person making the change.
    if "assignee_id" not in old_values:
        return
    if task.assignee_id and task.assignee_id != old_values["assignee_id"]:
        _notify_later(db, task.id, "assigned", actor.id, task.assignee_id)


register_crud(
    router,
    model=Task,
    entity_type="task",
    body_model=TaskIn,
    search_cols=[Task.name, Task.details],
    sortable={
        "due_at": Task.due_at,
        "name": Task.name,
        "priority": Task.priority,
        "status": Task.status,
        "created_at": Task.created_at,
        "completed_at": Task.completed_at,
    },
    filterable={
        "status": Task.status,
        "assignee_id": Task.assignee_id,
        "entity_type": Task.entity_type,
        "entity_id": Task.entity_id,
    },
    default_sort="due_at",
    required_any=["name"],
    has_extras=False,
    enrich=enrich,
    fk_checks={"assignee_id": User},
    body_validator=_validate_target,
    after_create=_notify_created,
    after_update=_after_update,
    enable_merge=False,  # nothing merges tasks; don't expose a hard-delete
)


def _add_interval(dt: datetime, every: int, unit: str) -> datetime:
    """dt advanced by every×unit, calendar-correct: Jan 31 + 1 month lands on
    Feb 28/29 (clamped to the target month's last day), not an invalid date.
    Hand-rolled because dateutil isn't a dependency."""
    if unit == "day":
        return dt + timedelta(days=every)
    if unit == "week":
        return dt + timedelta(weeks=every)
    months = every * 12 if unit == "year" else every
    total = dt.month - 1 + months
    year, month = dt.year + total // 12, total % 12 + 1
    return dt.replace(year=year, month=month, day=min(dt.day, calendar.monthrange(year, month)[1]))


def _next_due(due_at: datetime, every: int, unit: str) -> datetime | None:
    """The next occurrence's due time: at least one interval on from the old
    due date, then advanced repeatedly until it's in the future — a task
    completed months late must not spawn an instantly-overdue chain.

    None when there is no sane next date: the schema bounds due dates and
    intervals, but rows written before it did (or by an import) may not
    respect them, and date arithmetic past year 9999 raises. A series that
    has run off the end of the calendar simply stops. The catch-up loop is
    bounded too — a daily task last due in 1970 jumps straight to today's
    slot instead of stepping through twenty thousand days."""
    now = utcnow()
    try:
        nxt = _add_interval(due_at, every, unit)
        if nxt <= now and unit in ("day", "week"):
            step = timedelta(days=every * (7 if unit == "week" else 1))
            nxt += step * ((now - nxt) // step)
        hops = 0
        while nxt <= now:
            nxt = _add_interval(nxt, every, unit)
            hops += 1
            if hops > 10_000:
                return None
    except (OverflowError, ValueError):
        return None
    return nxt if nxt <= MAX_TASK_DATE else None


async def _get_task(db: AsyncSession, user: User, task_id: uuid.UUID) -> Task:
    t = (
        await db.execute(select(Task).where(Task.id == task_id, Task.org_id == user.org_id))
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    return t


@router.post("/{task_id}/complete")
async def complete_task(
    task_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # The open -> done transition is ONE conditional UPDATE. Two requests
    # completing the same task (a double tap, two devices) both used to read
    # "open" and both spawn a successor; now the database picks a single
    # winner — the loser's UPDATE waits on the row, re-checks the WHERE after
    # the winner commits, and matches nothing.
    now = utcnow()
    won = (
        await db.execute(
            update(Task)
            .where(Task.id == task_id, Task.org_id == user.org_id, Task.status != "done")
            .values(status="done", completed_at=now, updated_at=now)
            .returning(Task.id)
        )
    ).scalar_one_or_none()
    t = await _get_task(db, user, task_id)  # 404s an unknown id
    if won is not None:
        await log_activity(
            db, user.org_id, t.entity_type, t.entity_id, "task_completed", user.id,
            {"task_id": str(t.id), "name": t.name},
        )
        _notify_later(db, t.id, "completed", user.id, t.assignee_id)
        next_due = (
            _next_due(as_utc(t.due_at), t.repeat_every, t.repeat_unit)
            if t.repeat_every and t.repeat_unit and t.due_at
            else None
        )
        if next_due is not None:
            # Recurring: spawn the next occurrence as a fresh open task with
            # everything re-armed. Created directly — no created/assigned
            # notification, it's the same person's task continuing.
            db.add(
                Task(
                    org_id=t.org_id,
                    name=t.name,
                    details=t.details,
                    entity_type=t.entity_type,
                    entity_id=t.entity_id,
                    due_at=next_due,
                    priority=t.priority,
                    assignee_id=t.assignee_id,
                    created_by=t.created_by,
                    repeat_every=t.repeat_every,
                    repeat_unit=t.repeat_unit,
                )
            )
    await db.commit()  # visible before the client refetches
    return {
        "id": str(t.id),
        "status": t.status,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
    }


@router.post("/{task_id}/reopen")
async def reopen_task(
    task_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    t = await _get_task(db, user, task_id)
    # Reopening a recurring task that already spawned its successor leaves two
    # open occurrences — allowed; the user completes or deletes one of them.
    t.status = "open"
    t.completed_at = None
    # Re-arm the reminder — a reopened task is due again, and the sweep skips
    # anything already stamped as notified.
    t.due_notified_at = None
    await db.commit()  # visible before the client refetches
    return {"id": str(t.id), "status": t.status, "completed_at": None}
