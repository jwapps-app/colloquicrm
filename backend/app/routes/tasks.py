import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import Task, User, utcnow
from app.schemas import TaskIn
from app.services import colloqui
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


async def _validate_target(db, user, data):
    # A task can hang off a person/company/opportunity/lead — make sure the
    # target is a real in-org record before we store the pointer. Only act when
    # both parts are supplied together (create always sends both, or neither);
    # a partial PATCH of one alone isn't a link change to validate here.
    if "entity_type" in data and "entity_id" in data:
        await validate_entity_ref(
            db, user.org_id, data.get("entity_type"), data.get("entity_id")
        )


def _notify_created(task, actor):
    # Only a genuine hand-off notifies. A task you create for yourself (or leave
    # unassigned) needs no "you were assigned" ping and no team #tasks post —
    # only assigning it to someone else does.
    if task.assignee_id and task.assignee_id != actor.id:
        colloqui.schedule(
            colloqui.notify_task_event(
                task.id, "created", actor_id=actor.id, assignee_id=task.assignee_id
            )
        )


def _notify_assignment(task, old_values, actor):
    # Reassignment (not initial creation — that's the "created" event) to
    # someone other than the person making the change.
    if "assignee_id" not in old_values:
        return
    if task.assignee_id and task.assignee_id != old_values["assignee_id"]:
        colloqui.schedule(
            colloqui.notify_task_event(
                task.id, "assigned", actor_id=actor.id, assignee_id=task.assignee_id
            )
        )


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
    after_update=_notify_assignment,
)


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
    t = await _get_task(db, user, task_id)
    if t.status != "done":
        t.status = "done"
        t.completed_at = utcnow()
        await log_activity(
            db, user.org_id, t.entity_type, t.entity_id, "task_completed", user.id,
            {"task_id": str(t.id), "name": t.name},
        )
        colloqui.schedule(colloqui.notify_task_event(t.id, "completed"))
    await db.commit()  # visible before the client refetches
    return {"id": str(t.id), "status": t.status, "completed_at": t.completed_at.isoformat()}


@router.post("/{task_id}/reopen")
async def reopen_task(
    task_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    t = await _get_task(db, user, task_id)
    t.status = "open"
    t.completed_at = None
    await db.commit()  # visible before the client refetches
    return {"id": str(t.id), "status": t.status, "completed_at": None}
