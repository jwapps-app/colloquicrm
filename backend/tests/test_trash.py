"""Trash purge: never removes a restored record, takes satellites with a
truly expired one, and leaves linked tasks standing."""

from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select

from app.config import settings
from app.models import Attachment, Note, Person, Task, utcnow
from app.services.maintenance import purge_expired_trash

from .conftest import add, make_person


def _expired():
    return utcnow() - timedelta(days=settings.trash_retention_days + 5)


async def _with_satellites(world, person) -> Path:
    """A note, an attachment (row + file) and a linked task on the person."""
    stored_name = f"{person.id.hex}.txt"
    path = Path(settings.attachments_dir) / stored_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"contract")
    await add(
        Note(
            org_id=world.org.id,
            entity_type="person",
            entity_id=person.id,
            body="remember this",
            author_id=world.member.id,
        ),
        Attachment(
            org_id=world.org.id,
            entity_type="person",
            entity_id=person.id,
            filename="contract.txt",
            content_type="text/plain",
            size_bytes=8,
            stored_name=stored_name,
        ),
        Task(org_id=world.org.id, name="Follow up", entity_type="person", entity_id=person.id),
    )
    return path


async def _count(db_session, model) -> int:
    async with db_session() as db:
        return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def test_record_restored_between_selection_and_deletion_survives(
    client, world, db_session
):
    person = await make_person(world.org, deleted_at=_expired())
    path = await _with_satellites(world, person)
    seen = []

    async def restore_in_the_gap(entity_type, ids):
        seen.append((entity_type, list(ids)))
        if entity_type == "person":
            resp = await client.post(
                f"/api/v1/people/{person.id}/restore", headers=world.member_auth
            )
            assert resp.status_code == 200, resp.text

    assert await purge_expired_trash(_after_select=restore_in_the_gap) == 0
    assert seen == [("person", [person.id])]

    fetched = await client.get(f"/api/v1/people/{person.id}", headers=world.member_auth)
    assert fetched.status_code == 200
    assert await _count(db_session, Note) == 1
    assert await _count(db_session, Attachment) == 1
    assert path.is_file()
    async with db_session() as db:
        task = (await db.execute(select(Task))).scalar_one()
    assert (task.entity_type, task.entity_id) == ("person", person.id)


async def test_expired_record_is_purged_with_its_satellites_and_tasks_are_detached(
    world, db_session
):
    expired = await make_person(world.org, first_name="Gone", deleted_at=_expired())
    recent = await make_person(
        world.org, first_name="Recent", deleted_at=utcnow() - timedelta(days=1)
    )
    live = await make_person(world.org, first_name="Live")
    path = await _with_satellites(world, expired)
    await add(
        Note(org_id=world.org.id, entity_type="person", entity_id=live.id, body="keep me")
    )

    assert await purge_expired_trash() == 1

    async with db_session() as db:
        remaining = set((await db.execute(select(Person.id))).scalars())
        notes = (await db.execute(select(Note.body))).scalars().all()
        task = (await db.execute(select(Task))).scalar_one()
    assert remaining == {recent.id, live.id}
    assert notes == ["keep me"]
    assert await _count(db_session, Attachment) == 0
    assert not path.exists()
    # The task outlives the record, without a dangling pointer.
    assert (task.name, task.entity_type, task.entity_id) == ("Follow up", None, None)


async def test_restore_of_another_orgs_trash_is_404(client, world, other_world, db_session):
    person = await make_person(world.org, deleted_at=utcnow())
    resp = await client.post(
        f"/api/v1/people/{person.id}/restore", headers=other_world.admin_auth
    )
    assert resp.status_code == 404
    async with db_session() as db:
        assert (await db.get(Person, person.id)).deleted_at is not None
