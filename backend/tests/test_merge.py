"""Merging a duplicate into a record: what follows the survivor, what stays
with the (trashed) duplicate, and what a later purge removes."""

import uuid
from datetime import timedelta

from sqlalchemy import select

from app.models import (
    Activity,
    Attachment,
    CustomField,
    CustomFieldValue,
    EntityTag,
    Note,
    Person,
    PhoneEvent,
    Tag,
    Task,
    utcnow,
)
from app.db import Base
from app.services import entities
from app.services.maintenance import purge_expired_trash

from .conftest import add, make_person


async def _tags_of(db, person_id) -> set[str]:
    rows = await db.execute(
        select(Tag.name)
        .join(EntityTag, EntityTag.tag_id == Tag.id)
        .where(EntityTag.entity_type == "person", EntityTag.entity_id == person_id)
    )
    return {name for (name,) in rows}


async def _values_of(db, person_id) -> dict:
    rows = await db.execute(
        select(CustomField.name, CustomFieldValue.value)
        .join(CustomField, CustomField.id == CustomFieldValue.field_id)
        .where(CustomFieldValue.entity_type == "person", CustomFieldValue.entity_id == person_id)
    )
    return dict(rows.all())


async def _seed(world):
    keep = await make_person(world.org, first_name="Keep", work_email="keep@example.com")
    dupe = await make_person(world.org, first_name="Dupe", personal_email="dupe@example.com")
    shared, only_dupe = await add(
        Tag(org_id=world.org.id, name="shared"), Tag(org_id=world.org.id, name="only-dupe")
    )
    nickname, spouse = await add(
        CustomField(org_id=world.org.id, entity_type="person", name="Nickname"),
        CustomField(org_id=world.org.id, entity_type="person", name="Spouse"),
    )
    call = await add(
        PhoneEvent(
            org_id=world.org.id, rc_id=f"manual:{uuid.uuid4().hex}", kind="call",
            direction="outbound", other_number="", happened_at=utcnow(),
            entity_type="person", entity_id=dupe.id,
        )
    )
    target = {"entity_type": "person", "entity_id": dupe.id}
    await add(
        EntityTag(tag_id=shared.id, entity_type="person", entity_id=keep.id),
        EntityTag(tag_id=shared.id, **target),
        EntityTag(tag_id=only_dupe.id, **target),
        CustomFieldValue(field_id=nickname.id, entity_type="person", entity_id=keep.id, value="K"),
        CustomFieldValue(field_id=nickname.id, value="D", **target),
        CustomFieldValue(field_id=spouse.id, value="Sam", **target),
        Note(org_id=world.org.id, body="A note", phone_event_id=call.id, **target),
        Task(org_id=world.org.id, name="Follow up", **target),
        Activity(org_id=world.org.id, kind="created", payload={}, **target),
        Attachment(
            org_id=world.org.id, filename="a.pdf", content_type="application/pdf",
            size_bytes=1, stored_name=f"{uuid.uuid4().hex}.pdf", **target,
        ),
    )
    return keep, dupe


async def _merge(client, world, keep, dupe) -> dict:
    resp = await client.post(
        f"/api/v1/people/{keep.id}/merge",
        headers=world.admin_auth,
        json={"source_id": str(dupe.id)},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_merge_carries_the_duplicates_history_to_the_survivor(client, world, db_session):
    keep, dupe = await _seed(world)

    body = await _merge(client, world, keep, dupe)

    # Empty slots fill from the duplicate; the survivor's own values stand.
    assert (body["work_email"], body["personal_email"]) == ("keep@example.com", "dupe@example.com")
    assert body["first_name"] == "Keep"
    assert set(body["tags"]) == {"shared", "only-dupe"}
    async with db_session() as db:
        for model in (Note, Task, Attachment, PhoneEvent):
            owners = {eid for (eid,) in await db.execute(select(model.entity_id))}
            assert owners == {keep.id}, model.__name__
        moved = await db.execute(
            select(Activity.kind).where(Activity.entity_id == keep.id).order_by(Activity.kind)
        )
        assert [k for (k,) in moved] == ["created", "merged"]
        # A value the survivor lacked moves over; one it has is not overwritten.
        assert await _values_of(db, keep.id) == {"Nickname": "K", "Spouse": "Sam"}
        assert await _values_of(db, dupe.id) == {"Nickname": "D"}
        # The duplicate goes to Trash and keeps its tags, so a restore is whole.
        assert (await db.get(Person, dupe.id)).deleted_at is not None
        assert await _tags_of(db, dupe.id) == {"shared", "only-dupe"}
        assert await _tags_of(db, keep.id) == {"shared", "only-dupe"}


async def test_purging_the_trashed_duplicate_leaves_the_survivor_whole(client, world, db_session):
    keep, dupe = await _seed(world)
    await _merge(client, world, keep, dupe)
    async with db_session() as db:
        (await db.get(Person, dupe.id)).deleted_at = utcnow() - timedelta(days=400)
        await db.commit()

    assert await purge_expired_trash() == 1

    async with db_session() as db:
        assert await db.get(Person, dupe.id) is None
        assert await _tags_of(db, dupe.id) == set() and await _values_of(db, dupe.id) == {}
        assert await _tags_of(db, keep.id) == {"shared", "only-dupe"}
        assert await _values_of(db, keep.id) == {"Nickname": "K", "Spouse": "Sam"}
        for model in (Note, Task, Attachment, PhoneEvent):
            owners = {eid for (eid,) in await db.execute(select(model.entity_id))}
            assert owners == {keep.id}, model.__name__


async def test_merge_refuses_itself_and_other_orgs(client, world, other_world):
    mine = await make_person(world.org)
    theirs = await make_person(other_world.org)
    for source, status in ((mine, 422), (theirs, 404)):
        resp = await client.post(
            f"/api/v1/people/{mine.id}/merge",
            headers=world.admin_auth,
            json={"source_id": str(source.id)},
        )
        assert resp.status_code == status


def test_every_table_that_points_at_a_record_is_registered():
    """A table with (entity_type, entity_id) hangs off records with no foreign
    key. If it isn't in entities.SATELLITES, a merge strands its rows and a
    purge orphans them — so adding one without registering it fails here."""
    # A rule's run log: its rows go with the rule, and outliving the record
    # is what stops the rule firing twice.
    exempt = {"automation_fires"}
    pointing = {
        table.name
        for table in Base.metadata.tables.values()
        if {"entity_type", "entity_id"} <= set(table.c.keys())
    }
    registered = {sat.model.__table__.name for sat in entities.SATELLITES}
    assert pointing - exempt == registered


async def test_record_types_are_published(client, world):
    resp = await client.get("/api/v1/options/record-types", headers=world.member_auth)
    assert resp.status_code == 200
    assert resp.json() == [
        {"type": "person", "plural": "people", "correspondent": True, "logs_calls": True},
        {"type": "lead", "plural": "leads", "correspondent": True, "logs_calls": True},
        {"type": "company", "plural": "companies", "correspondent": True, "logs_calls": False},
        {
            "type": "opportunity", "plural": "opportunities", "correspondent": False,
            "logs_calls": False,
        },
    ]
