"""Note relinking permissions, record-consistency of call links, list bounds."""

from datetime import timedelta

from app.models import Note, PhoneEvent, utcnow

from .conftest import add, make_person


def _sms(org, number: str, **fields) -> PhoneEvent:
    return PhoneEvent(
        org_id=org.id,
        rc_id=f"rc-{number}",
        kind="sms",
        direction="inbound",
        other_number=number,
        happened_at=utcnow(),
        **fields,
    )


async def test_non_author_member_cannot_relink_a_note(client, world, db_session):
    person = await make_person(world.org, mobile_phone="+15550100001")
    event = await add(_sms(world.org, "+15550100001"))
    note = await add(
        Note(
            org_id=world.org.id,
            entity_type="person",
            entity_id=person.id,
            body="Admin's note",
            author_id=world.admin.id,
        )
    )
    resp = await client.patch(
        f"/api/v1/notes/{note.id}",
        headers=world.member_auth,
        json={"phone_event_id": str(event.id)},
    )
    assert resp.status_code == 403
    async with db_session() as db:
        assert (await db.get(Note, note.id)).phone_event_id is None

    # The author (and an admin) still can.
    ok = await client.patch(
        f"/api/v1/notes/{note.id}",
        headers=world.admin_auth,
        json={"phone_event_id": str(event.id)},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["phone_event_id"] == str(event.id)


async def test_relinking_to_an_event_on_another_record_is_422(client, world, db_session):
    person = await make_person(world.org, mobile_phone="+15550100001")
    await make_person(world.org, first_name="Other", mobile_phone="+15550100002")
    unrelated = await add(_sms(world.org, "+15550100002"))
    note = await add(
        Note(
            org_id=world.org.id,
            entity_type="person",
            entity_id=person.id,
            body="Mine",
            author_id=world.member.id,
        )
    )
    resp = await client.patch(
        f"/api/v1/notes/{note.id}",
        headers=world.member_auth,
        json={"phone_event_id": str(unrelated.id)},
    )
    assert resp.status_code == 422
    async with db_session() as db:
        assert (await db.get(Note, note.id)).phone_event_id is None


async def test_creating_a_note_on_an_event_of_another_record_is_422(client, world):
    person = await make_person(world.org, mobile_phone="+15550100001")
    other = await make_person(world.org, first_name="Other", mobile_phone="+15550100002")
    unrelated = await add(
        _sms(world.org, "+15550100002", entity_type="person", entity_id=other.id)
    )
    payload = {
        "entity_type": "person",
        "entity_id": str(person.id),
        "body": "Follow-up",
        "phone_event_id": str(unrelated.id),
    }
    resp = await client.post("/api/v1/notes", headers=world.member_auth, json=payload)
    assert resp.status_code == 422

    payload["entity_id"] = str(other.id)
    ok = await client.post("/api/v1/notes", headers=world.member_auth, json=payload)
    assert ok.status_code == 201, ok.text


async def test_note_list_is_bounded_by_limit(client, world):
    person = await make_person(world.org)
    now = utcnow()
    await add(
        *(
            Note(
                org_id=world.org.id,
                entity_type="person",
                entity_id=person.id,
                body=f"note {i}",
                author_id=world.member.id,
                created_at=now - timedelta(minutes=i),
            )
            for i in range(5)
        )
    )
    params = {"entity_type": "person", "entity_id": str(person.id)}
    resp = await client.get(
        "/api/v1/notes", headers=world.member_auth, params={**params, "limit": 2}
    )
    assert resp.status_code == 200
    assert [n["body"] for n in resp.json()["items"]] == ["note 0", "note 1"]
    everything = await client.get("/api/v1/notes", headers=world.member_auth, params=params)
    assert len(everything.json()["items"]) == 5


async def test_notes_of_another_org_are_invisible(client, world, other_world):
    person = await make_person(world.org)
    note = await add(
        Note(
            org_id=world.org.id,
            entity_type="person",
            entity_id=person.id,
            body="private",
            author_id=world.admin.id,
        )
    )
    listed = await client.get(
        "/api/v1/notes",
        headers=other_world.admin_auth,
        params={"entity_type": "person", "entity_id": str(person.id)},
    )
    assert listed.json() == {"items": []}
    relink = await client.patch(
        f"/api/v1/notes/{note.id}", headers=other_world.admin_auth, json={"phone_event_id": None}
    )
    assert relink.status_code == 404
    created = await client.post(
        "/api/v1/notes",
        headers=other_world.admin_auth,
        json={"entity_type": "person", "entity_id": str(person.id), "body": "x"},
    )
    assert created.status_code == 404
