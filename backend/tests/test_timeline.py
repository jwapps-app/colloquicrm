"""One record's timeline: the same sources, shape and order as the feed,
scoped to what the record answers for."""

import uuid
from datetime import timedelta

from app.models import Activity, EmailMessage, EmailParticipant, Note, PhoneEvent, utcnow

from .conftest import add, make_company, make_opportunity, make_person


async def _timeline(client, auth, entity_type, entity_id, **params) -> dict:
    resp = await client.get(
        "/api/v1/timeline",
        headers=auth,
        params={"entity_type": entity_type, "entity_id": str(entity_id), **params},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _email(org, when, *participants, subject="Hello") -> EmailMessage:
    """An archived message; participants are (address, direct) pairs."""
    msg = await add(
        EmailMessage(
            org_id=org.id,
            gmail_id=uuid.uuid4().hex[:16],
            rfc_message_id=f"<{uuid.uuid4().hex}@example.com>",
            subject=subject,
            snippet="…",
            from_email=participants[0][0],
            sent_at=when,
        )
    )
    await add(
        *[
            EmailParticipant(email_id=msg.id, email=addr, kind="to", direct=direct)
            for addr, direct in participants
        ]
    )
    return msg


async def _call(org, when, number, **fields) -> PhoneEvent:
    return await add(
        PhoneEvent(
            org_id=org.id,
            rc_id=f"call:{uuid.uuid4().hex}",
            kind="call",
            direction="inbound",
            other_number=number,
            happened_at=when,
            **fields,
        )
    )


async def test_person_timeline_merges_every_source_newest_first(client, world):
    person = await make_person(
        world.org, work_email="pat@example.com", mobile_phone="(555) 123-4567"
    )
    stranger = await make_person(world.org, first_name="Sam", work_email="sam@example.com")
    now = utcnow()
    mail = await _email(world.org, now - timedelta(minutes=1), ("pat@example.com", True))
    await _email(world.org, now, ("sam@example.com", True))  # someone else's
    await _email(world.org, now, ("pat@example.com", False))  # only cc'd on a blast
    call = await _call(world.org, now - timedelta(minutes=2), "+15551234567")
    await _call(world.org, now, "+15559999999")  # not this person's number
    note, about_call, _ = await add(
        Note(
            org_id=world.org.id, entity_type="person", entity_id=person.id, body="Standalone",
            author_id=world.member.id, created_at=now - timedelta(minutes=3),
        ),
        Note(
            org_id=world.org.id, entity_type="person", entity_id=person.id, body="About the call",
            author_id=world.member.id, phone_event_id=call.id, created_at=now,
        ),
        Note(org_id=world.org.id, entity_type="person", entity_id=stranger.id, body="Not Pat's"),
    )
    activity = await add(
        Activity(
            org_id=world.org.id, entity_type="person", entity_id=person.id, kind="created",
            actor_id=world.admin.id, payload={}, created_at=now - timedelta(minutes=4),
        )
    )

    body = await _timeline(client, world.member_auth, "person", person.id)

    assert set(body) == {"items", "page", "page_size", "has_more"}
    assert body["has_more"] is False
    assert [(i["type"], i["id"]) for i in body["items"]] == [
        ("email", str(mail.id)),
        ("call", str(call.id)),
        ("note", str(note.id)),
        ("activity", str(activity.id)),
    ]
    by_type = {i["type"]: i for i in body["items"]}
    # The note about the call rides under the call, not beside it.
    assert [n["id"] for n in by_type["call"]["notes"]] == [str(about_call.id)]
    assert by_type["call"]["notes"][0]["author_name"] == "Max Member"
    # The record page needs to know whose note it is.
    assert by_type["note"]["author_id"] == str(world.member.id)
    assert by_type["note"]["phone_event_id"] is None


async def test_kind_filter_and_paging(client, world):
    person = await make_person(world.org)
    now = utcnow()
    await add(
        *[
            Note(
                org_id=world.org.id, entity_type="person", entity_id=person.id, body=f"n{i}",
                created_at=now - timedelta(minutes=i),
            )
            for i in range(5)
        ],
        Activity(
            org_id=world.org.id, entity_type="person", entity_id=person.id, kind="updated",
            payload={}, created_at=now,
        ),
    )
    first = await _timeline(client, world.member_auth, "person", person.id, kind="note", page_size=3)
    second = await _timeline(
        client, world.member_auth, "person", person.id, kind="note", page_size=3, page=2
    )
    assert [i["body"] for i in first["items"]] == ["n0", "n1", "n2"]
    assert first["has_more"] is True
    assert [i["body"] for i in second["items"]] == ["n3", "n4"]
    assert second["has_more"] is False


async def test_company_timeline_rolls_up_its_people(client, world):
    company = await make_company(world.org, email_domain="globex.com")
    person = await make_person(
        world.org, company_id=company.id, personal_email="pat@gmail.com", work_phone="555-000-1111"
    )
    now = utcnow()
    by_domain = await _email(world.org, now, ("anyone@globex.com", True))
    by_person = await _email(world.org, now - timedelta(minutes=1), ("pat@gmail.com", True))
    by_number = await _call(world.org, now - timedelta(minutes=2), "+15550001111")
    on_person = await _call(
        world.org, now - timedelta(minutes=3), "", entity_type="person", entity_id=person.id
    )

    body = await _timeline(client, world.member_auth, "company", company.id)

    assert [i["id"] for i in body["items"]] == [
        str(by_domain.id), str(by_person.id), str(by_number.id), str(on_person.id),
    ]


async def test_opportunity_timeline_has_notes_and_activity_only(client, world):
    opp = await make_opportunity(world.org)
    await _email(world.org, utcnow(), ("pat@example.com", True))
    await add(Note(org_id=world.org.id, entity_type="opportunity", entity_id=opp.id, body="Deal note"))

    body = await _timeline(client, world.member_auth, "opportunity", opp.id)

    assert [i["type"] for i in body["items"]] == ["note"]


async def test_timeline_is_scoped_to_the_callers_org(client, world, other_world):
    theirs = await make_person(other_world.org)
    resp = await client.get(
        "/api/v1/timeline",
        headers=world.member_auth,
        params={"entity_type": "person", "entity_id": str(theirs.id)},
    )
    assert resp.status_code == 404
    resp = await client.get(
        "/api/v1/timeline",
        headers=world.member_auth,
        params={"entity_type": "widget", "entity_id": str(theirs.id)},
    )
    assert resp.status_code == 422
