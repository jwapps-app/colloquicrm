"""CSV import: unknown columns, repeated merges, server-owned columns, row cap."""

import asyncio
import uuid

from sqlalchemy import select

from app.models import CustomField, CustomFieldValue, EntityTag, Person
from app.routes.imports import MAX_COMMIT_ROWS
from app.services import background

from .conftest import make_person

CSV = b"First Name,Last Name,Favorite Color\nAda,Lovelace,Blue\n"


async def _preview(client, auth, content: bytes = CSV, import_type: str = "people"):
    resp = await client.post(
        "/api/v1/imports/preview",
        headers=auth,
        data={"type": import_type},
        files={"file": ("import.csv", content, "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _commit(client, auth, rows: list[dict], import_type: str = "people") -> dict:
    """Commit and wait for the background job; returns its final status."""
    resp = await client.post(
        "/api/v1/imports/commit", headers=auth, json={"type": import_type, "rows": rows}
    )
    assert resp.status_code == 202, resp.text
    running = [t for t in background._tasks if not t.done()]
    if running:
        await asyncio.wait(running, timeout=30)
    status = await client.get(f"/api/v1/imports/jobs/{resp.json()['job_id']}", headers=auth)
    return status.json()


async def _field_values(db_session, org, entity_id) -> dict[str, str]:
    async with db_session() as db:
        rows = await db.execute(
            select(CustomField.name, CustomFieldValue.value)
            .join(CustomFieldValue, CustomFieldValue.field_id == CustomField.id)
            .where(CustomField.org_id == org.id, CustomFieldValue.entity_id == entity_id)
        )
        return {name: value for name, value in rows}


async def test_unknown_header_is_kept_as_a_custom_field_for_an_admin(
    client, world, db_session
):
    preview = await _preview(client, world.admin_auth)
    assert preview["unmapped_headers"] == ["Favorite Color"]
    assert preview["discarded_headers"] == []
    assert preview["rows"][0]["custom_fields"] == {"Favorite Color": "Blue"}

    job = await _commit(client, world.admin_auth, preview["rows"])
    assert (job["status"], job["created"], job["error"]) == ("done", 1, None)
    assert job["custom_fields_created"] == ["Favorite Color"]
    async with db_session() as db:
        person = (await db.execute(select(Person).where(Person.first_name == "Ada"))).scalar_one()
    assert await _field_values(db_session, world.org, person.id) == {"Favorite Color": "Blue"}


async def test_unknown_header_is_reported_as_discarded_for_a_member(
    client, world, db_session
):
    preview = await _preview(client, world.member_auth)
    assert preview["discarded_headers"] == ["Favorite Color"]
    assert preview["unmapped_headers"] == []
    assert preview["rows"][0]["custom_fields"] == {}

    # The commit side enforces the same policy even if the client re-adds it.
    rows = [{**preview["rows"][0], "custom_fields": {"Favorite Color": "Blue"}}]
    job = await _commit(client, world.member_auth, rows)
    assert (job["status"], job["created"]) == ("done", 1)
    async with db_session() as db:
        fields = (await db.execute(select(CustomField))).scalars().all()
    assert fields == []


async def test_two_rows_merging_the_same_field_into_one_person_commit_cleanly(
    client, world, db_session
):
    person = await make_person(world.org, first_name="Ada", last_name="Lovelace")
    rows = [
        {
            "action": "merge",
            "merge_id": str(person.id),
            "data": {"first_name": "Ada"},
            "custom_fields": {"Favorite Color": color},
        }
        for color in ("Blue", "Green")
    ]
    job = await _commit(client, world.admin_auth, rows)
    assert job["error"] is None
    assert (job["status"], job["merged"], job["created"]) == ("done", 2, 0)
    # A merge only fills blanks: the first value stays.
    assert await _field_values(db_session, world.org, person.id) == {"Favorite Color": "Blue"}


async def test_two_rows_merging_the_same_tag_into_one_person_commit_cleanly(
    client, world, db_session
):
    person = await make_person(world.org, first_name="Ada", last_name="Lovelace")
    rows = [
        {"action": "merge", "merge_id": str(person.id), "data": {}, "tags": ["VIP", " VIP "]}
        for _ in range(2)
    ]
    job = await _commit(client, world.member_auth, rows)
    assert job["error"] is None
    assert (job["status"], job["merged"]) == ("done", 2)
    async with db_session() as db:
        links = (
            await db.execute(select(EntityTag).where(EntityTag.entity_id == person.id))
        ).all()
    assert len(links) == 1


async def test_row_cannot_override_server_owned_columns(
    client, world, other_world, db_session
):
    forced_id = uuid.uuid4()
    rows = [
        {
            "data": {
                "first_name": "Eve",
                "last_name": "Intruder",
                "id": str(forced_id),
                "org_id": str(other_world.org.id),
                "deleted_at": "2020-01-01",
                "interaction_count": 999,
            }
        }
    ]
    job = await _commit(client, world.member_auth, rows)
    assert (job["status"], job["created"], job["error"]) == ("done", 1, None)
    async with db_session() as db:
        people = (await db.execute(select(Person))).scalars().all()
    assert len(people) == 1
    eve = people[0]
    assert eve.org_id == world.org.id
    assert eve.id != forced_id
    assert eve.deleted_at is None
    assert eve.interaction_count == 0


async def test_more_than_the_row_cap_is_422(client, world, db_session):
    from app.models import ImportJob

    resp = await client.post(
        "/api/v1/imports/commit",
        headers=world.member_auth,
        json={"type": "people", "rows": [{}] * (MAX_COMMIT_ROWS + 1)},
    )
    assert resp.status_code == 422
    assert "Too many rows" in resp.json()["detail"]
    async with db_session() as db:
        assert (await db.execute(select(ImportJob))).all() == []


async def test_import_job_of_another_org_is_not_readable(client, world, other_world):
    job = await _commit(client, world.member_auth, [{"data": {"first_name": "Solo"}}])
    resp = await client.get(
        f"/api/v1/imports/jobs/{job['job_id']}", headers=other_world.admin_auth
    )
    assert resp.status_code == 404
