"""Feed paging: the response envelope, a total order, and the page cap."""

from datetime import timedelta

from app.models import Activity, Note, utcnow
from app.routes.feed import _MAX_PAGE

from .conftest import add, make_person


async def _page(client, auth, **params) -> dict:
    resp = await client.get("/api/v1/feed", headers=auth, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_feed_keeps_its_page_based_envelope(client, world):
    person = await make_person(world.org, first_name="Pat", last_name="Person")
    now = utcnow()
    await add(
        Note(
            org_id=world.org.id,
            entity_type="person",
            entity_id=person.id,
            body="Newest",
            author_id=world.member.id,
            created_at=now,
        ),
        Activity(
            org_id=world.org.id,
            entity_type="person",
            entity_id=person.id,
            kind="created",
            actor_id=world.admin.id,
            payload={},
            created_at=now - timedelta(minutes=1),
        ),
    )
    body = await _page(client, world.member_auth, page=1, page_size=1)
    assert set(body) == {"items", "page", "page_size", "has_more"}
    assert (body["page"], body["page_size"], body["has_more"]) == (1, 1, True)
    (note,) = body["items"]
    assert set(note) == {
        "type", "at", "id", "body", "author_name", "entity_type", "entity_id", "related",
    }
    assert (note["type"], note["body"], note["author_name"]) == ("note", "Newest", "Max Member")
    assert note["related"] == [
        {"entity_type": "person", "entity_id": str(person.id), "label": "Pat Person"}
    ]

    second = await _page(client, world.member_auth, page=2, page_size=1)
    assert (second["page"], second["has_more"]) == (2, False)
    assert [i["type"] for i in second["items"]] == ["activity"]
    assert second["items"][0]["actor_name"] == "Ada Admin"


async def test_order_is_stable_across_pages_when_timestamps_tie(client, world):
    person = await make_person(world.org)
    stamp = utcnow() - timedelta(hours=1)  # one bulk import, one timestamp
    notes = [
        Note(
            org_id=world.org.id,
            entity_type="person",
            entity_id=person.id,
            body=f"n{i}",
            created_at=stamp,
        )
        for i in range(25)
    ]
    activities = [
        Activity(
            org_id=world.org.id,
            entity_type="person",
            entity_id=person.id,
            kind="updated",
            payload={},
            created_at=stamp,
        )
        for i in range(10)
    ]
    await add(*notes, *activities)
    # The documented total order: newest first, then source, then id.
    expected = [str(a.id) for a in sorted(activities, key=lambda a: str(a.id))] + [
        str(n.id) for n in sorted(notes, key=lambda n: str(n.id))
    ]

    seen: list[str] = []
    for page in range(1, 5):
        body = await _page(client, world.member_auth, page=page, page_size=10)
        assert body["has_more"] is (page < 4)
        seen.extend(item["id"] for item in body["items"])
    assert len(seen) == len(set(seen)) == 35
    assert seen == expected
    # Asking again gives the same pages.
    again = await _page(client, world.member_auth, page=2, page_size=10)
    assert [item["id"] for item in again["items"]] == expected[10:20]


async def test_page_beyond_the_cap_is_an_empty_envelope(client, world):
    person = await make_person(world.org)
    await add(Note(org_id=world.org.id, entity_type="person", entity_id=person.id, body="x"))
    body = await _page(client, world.member_auth, page=_MAX_PAGE + 1, page_size=1)
    assert body == {"items": [], "page": _MAX_PAGE + 1, "page_size": 1, "has_more": False}


async def test_feed_shows_only_the_callers_org(client, world, other_world):
    person = await make_person(world.org)
    await add(Note(org_id=world.org.id, entity_type="person", entity_id=person.id, body="ours"))
    assert (await _page(client, other_world.admin_auth))["items"] == []
    assert len((await _page(client, world.admin_auth))["items"]) == 1
