import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import Task, User, utcnow
from app.schemas import TaskIn
from app.services import colloqui
from app.services.common import display_name_map, log_activity
from app.services.crud import register_crud

router = APIRouter()


async def enrich(db, user, dicts):
    names = await display_name_map(
        db, {d.get("assignee_id") for d in dicts} | {d.get("created_by") for d in dicts}
    )
    for d in dicts:
        d["assignee_name"] = names.get(d.get("assignee_id"))
        d["created_by_name"] = names.get(d.get("created_by"))


def _notify_created(task, actor):
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
