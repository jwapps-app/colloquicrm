"""Recurrence validation, exclusive completion, reminder re-arming, and
notifications that wait for the commit."""

import asyncio
from datetime import timedelta

from sqlalchemy import select

from app.models import Task, utcnow
from app.routes import tasks as task_routes
from app.services import background, colloqui
from app.services.background import pending_after_commit

from .conftest import add


async def _settle() -> None:
    running = [t for t in background._tasks if not t.done()]
    if running:
        await asyncio.wait(running, timeout=10)


def _recurring(world, **fields) -> Task:
    fields.setdefault("due_at", utcnow() + timedelta(days=1))
    return Task(
        org_id=world.org.id,
        name="Weekly check-in",
        repeat_every=1,
        repeat_unit="week",
        assignee_id=world.member.id,
        created_by=world.member.id,
        **fields,
    )


async def test_half_a_recurrence_is_422_and_both_nulls_clear_it(client, world, db_session):
    task = await add(_recurring(world))
    url = f"/api/v1/tasks/{task.id}"

    for half in ({"repeat_every": None}, {"repeat_unit": None}, {"repeat_every": 2}):
        resp = await client.patch(url, headers=world.member_auth, json=half)
        assert resp.status_code == 422, half
    async with db_session() as db:
        stored = await db.get(Task, task.id)
        assert (stored.repeat_every, stored.repeat_unit) == (1, "week")

    cleared = await client.patch(
        url, headers=world.member_auth, json={"repeat_every": None, "repeat_unit": None}
    )
    assert cleared.status_code == 200, cleared.text
    assert (cleared.json()["repeat_every"], cleared.json()["repeat_unit"]) == (None, None)


async def test_concurrent_completions_spawn_exactly_one_successor(client, world, db_session):
    task = await add(_recurring(world))
    responses = await asyncio.gather(
        *(
            client.post(f"/api/v1/tasks/{task.id}/complete", headers=world.member_auth)
            for _ in range(6)
        )
    )
    assert {r.status_code for r in responses} == {200}
    assert {r.json()["status"] for r in responses} == {"done"}
    async with db_session() as db:
        rows = (await db.execute(select(Task).order_by(Task.due_at))).scalars().all()
    assert [t.status for t in rows] == ["done", "open"]
    successor = rows[1]
    assert successor.due_at == task.due_at + timedelta(weeks=1)
    assert (successor.repeat_every, successor.repeat_unit) == (1, "week")
    assert successor.due_notified_at is None


async def test_postponing_a_task_rearms_its_fired_reminder(client, world, db_session):
    fired = utcnow() - timedelta(minutes=5)
    task = await add(
        Task(
            org_id=world.org.id,
            name="Call back",
            due_at=utcnow() - timedelta(minutes=1),
            due_notified_at=fired,
            assignee_id=world.member.id,
        )
    )
    # An edit that doesn't move the schedule leaves the reminder spent…
    renamed = await client.patch(
        f"/api/v1/tasks/{task.id}", headers=world.member_auth, json={"name": "Call back today"}
    )
    assert renamed.status_code == 200, renamed.text
    async with db_session() as db:
        assert (await db.get(Task, task.id)).due_notified_at == fired

    # …postponing it arms it again.
    new_due = (utcnow() + timedelta(days=2)).isoformat()
    moved = await client.patch(
        f"/api/v1/tasks/{task.id}", headers=world.member_auth, json={"due_at": new_due}
    )
    assert moved.status_code == 200, moved.text
    async with db_session() as db:
        assert (await db.get(Task, task.id)).due_notified_at is None


class _Notifier:
    """Stands in for colloqui.notify_task_event; records what it was told and
    whether the task was already durable when it ran."""

    def __init__(self, session_factory):
        self.calls: list[tuple] = []
        self.session_factory = session_factory

    async def __call__(self, task_id, event, actor_id=None, assignee_id=None):
        async with self.session_factory() as db:
            visible = (await db.get(Task, task_id)) is not None
        self.calls.append((event, assignee_id, visible))


async def test_assignment_notifies_once_and_only_after_the_commit(
    client, world, db_session, monkeypatch
):
    notifier = _Notifier(db_session)
    monkeypatch.setattr(colloqui, "notify_task_event", notifier)
    resp = await client.post(
        "/api/v1/tasks",
        headers=world.admin_auth,
        json={"name": "Send the proposal", "assignee_id": str(world.member.id)},
    )
    assert resp.status_code == 201, resp.text
    await _settle()
    assert notifier.calls == [("created", world.member.id, True)]


async def test_a_create_that_fails_validation_notifies_nobody(
    client, world, other_world, db_session, monkeypatch
):
    notifier = _Notifier(db_session)
    monkeypatch.setattr(colloqui, "notify_task_event", notifier)
    resp = await client.post(
        "/api/v1/tasks",
        headers=world.admin_auth,
        json={
            "name": "Follow up",
            "assignee_id": str(world.member.id),
            "entity_type": "person",
            "entity_id": str(other_world.member.id),  # not a person in this org
        },
    )
    assert resp.status_code == 404
    await _settle()
    assert notifier.calls == []
    async with db_session() as db:
        assert (await db.execute(select(Task))).all() == []


async def test_a_rolled_back_write_drops_its_queued_notification(
    world, db_session, monkeypatch
):
    notifier = _Notifier(db_session)
    monkeypatch.setattr(colloqui, "notify_task_event", notifier)

    async with db_session() as db:
        task = Task(org_id=world.org.id, name="Doomed", assignee_id=world.member.id)
        db.add(task)
        await db.flush()
        task_routes._notify_created(db, task, world.admin)
        assert pending_after_commit(db) == 1
        await _settle()
        assert notifier.calls == []  # nothing goes out mid-transaction
        await db.rollback()
        assert pending_after_commit(db) == 0
    await _settle()
    assert notifier.calls == []

    async with db_session() as db:
        task = Task(org_id=world.org.id, name="Kept", assignee_id=world.member.id)
        db.add(task)
        await db.flush()
        task_routes._notify_created(db, task, world.admin)
        await db.commit()
    await _settle()
    assert notifier.calls == [("created", world.member.id, True)]


async def test_task_of_another_org_is_unreachable(client, world, other_world):
    task = await add(Task(org_id=world.org.id, name="Ours"))
    for method, path in (
        ("GET", f"/api/v1/tasks/{task.id}"),
        ("POST", f"/api/v1/tasks/{task.id}/complete"),
        ("POST", f"/api/v1/tasks/{task.id}/reopen"),
    ):
        resp = await client.request(method, path, headers=other_world.admin_auth)
        assert resp.status_code == 404, path
