"""Interaction count and last-contacted date: people who arrive after their
mail and calls were already synced, shared phone numbers, imports, and an
imported date that has to survive the sync recomputing the column."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url

from app.models import (
    EmailMessage,
    EmailParticipant,
    ImportJob,
    Person,
    PhoneEvent,
    utcnow,
)
from app.services import background
from app.services.importer import run_import_job
from app.services.interactions import update_person_aggregates

from .conftest import TEST_DATABASE_URL, add, make_lead, make_person, run_alembic

PREVIOUS_HEAD = "c4e8a1f07b35"


async def _stored_email(org, address: str, sent_at: datetime | None = None) -> EmailMessage:
    """An archived message the address took a direct part in."""
    msg = EmailMessage(
        org_id=org.id,
        gmail_id=uuid.uuid4().hex[:16],
        rfc_message_id=f"<{uuid.uuid4().hex}@mail.example>",
        subject="Hello",
        from_email=address,
        sent_at=sent_at or utcnow() - timedelta(days=3),
    )
    await add(msg)
    await add(EmailParticipant(email_id=msg.id, email=address, kind="from", direct=True))
    return msg


async def _stored_call(org, number: str, happened_at: datetime | None = None) -> PhoneEvent:
    return await add(
        PhoneEvent(
            org_id=org.id,
            rc_id=f"rc-{uuid.uuid4().hex}",
            kind="call",
            direction="inbound",
            other_number=number,
            happened_at=happened_at or utcnow() - timedelta(days=1),
        )
    )


async def _person(db_session, person_id) -> Person:
    async with db_session() as db:
        return await db.get(Person, uuid.UUID(str(person_id)))


async def _recompute(db_session, org, *person_ids) -> None:
    async with db_session() as db:
        await update_person_aggregates(db, org.id, {uuid.UUID(str(p)) for p in person_ids})
        await db.commit()


# --- a person who arrives after their history ---------------------------------


async def test_person_created_over_the_api_starts_with_their_synced_history(
    client, world, db_session
):
    sent_at = utcnow() - timedelta(days=3)
    await _stored_email(world.org, "client@customer.example", sent_at)
    await _stored_call(world.org, "+15551234567", sent_at - timedelta(days=5))

    resp = await client.post(
        "/api/v1/people",
        headers=world.member_auth,
        json={
            "first_name": "Cleo",
            "work_email": "Client@Customer.example",
            "mobile_phone": "(555) 123-4567",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Same keys, same types as ever: an int and a nullable ISO timestamp.
    assert body["interaction_count"] == 2
    assert datetime.fromisoformat(body["last_contacted_at"]) == sent_at
    stored = await _person(db_session, body["id"])
    assert (stored.interaction_count, stored.last_contacted_at) == (2, sent_at)


async def test_person_created_with_no_history_is_untouched(client, world):
    resp = await client.post(
        "/api/v1/people", headers=world.member_auth, json={"first_name": "Nova"}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["interaction_count"] == 0
    assert resp.json()["last_contacted_at"] is None


async def test_converted_lead_starts_with_the_history_behind_its_address(
    client, world, db_session
):
    called_at = utcnow() - timedelta(hours=6)
    await _stored_email(world.org, "lee@prospect.example")
    await _stored_call(world.org, "+15557654321", called_at)
    lead = await make_lead(world.org, email="lee@prospect.example", work_phone="555-765-4321")

    resp = await client.post(
        f"/api/v1/leads/{lead.id}/convert", headers=world.member_auth, json={}
    )
    assert resp.status_code == 200, resp.text
    person = await _person(db_session, resp.json()["person_id"])
    assert (person.interaction_count, person.last_contacted_at) == (2, called_at)


# --- one number, several people -----------------------------------------------


async def test_manually_logged_call_counts_for_everyone_sharing_the_number(
    client, world, db_session
):
    logged_on = await make_person(world.org, first_name="Front", work_phone="(555) 200-3000")
    shares_it = await make_person(world.org, first_name="Desk", mobile_phone="+1 555 200 3000")
    unrelated = await make_person(world.org, first_name="Else", work_phone="555-200-3001")

    resp = await client.post(
        "/api/v1/notes",
        headers=world.member_auth,
        json={
            "entity_type": "person",
            "entity_id": str(logged_on.id),
            "body": "Talked through the renewal",
            "log_call": True,
        },
    )
    assert resp.status_code == 201, resp.text
    counts = [
        (await _person(db_session, p.id)).interaction_count
        for p in (logged_on, shares_it, unrelated)
    ]
    assert counts == [1, 1, 0]
    assert (await _person(db_session, shares_it.id)).last_contacted_at is not None

    # Deleting the note removes the call — for both of them.
    resp = await client.delete(f"/api/v1/notes/{resp.json()['id']}", headers=world.member_auth)
    assert resp.status_code in (200, 204), resp.text
    for p in (logged_on, shares_it):
        after = await _person(db_session, p.id)
        assert (after.interaction_count, after.last_contacted_at) == (0, None)


# --- imports -------------------------------------------------------------------


async def _commit_import(client, auth, rows: list[dict]) -> dict:
    resp = await client.post(
        "/api/v1/imports/commit", headers=auth, json={"type": "people", "rows": rows}
    )
    assert resp.status_code == 202, resp.text
    running = [t for t in background._tasks if not t.done()]
    if running:
        await asyncio.wait(running, timeout=30)
    status = await client.get(f"/api/v1/imports/jobs/{resp.json()['job_id']}", headers=auth)
    return status.json()


async def _preview(client, auth, content: bytes) -> list[dict]:
    resp = await client.post(
        "/api/v1/imports/preview",
        headers=auth,
        data={"type": "people"},
        files={"file": ("import.csv", content, "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["rows"]


async def test_import_recomputes_the_people_it_created_and_merged(client, world, db_session):
    sent_at = utcnow() - timedelta(days=4)
    await _stored_email(world.org, "new@customer.example", sent_at)
    await _stored_email(world.org, "known@customer.example", sent_at)
    # Already in the CRM, but under an address with no mail; the import merges
    # in the one that has some.
    known = await make_person(world.org, first_name="Kay", last_name="Known")

    rows = await _preview(
        client,
        world.admin_auth,
        b"First Name,Last Name,Work Email\n"
        b"Ned,New,new@customer.example\n"
        b"Kay,Known,known@customer.example\n",
    )
    rows[1]["action"], rows[1]["merge_id"] = "merge", str(known.id)
    job = await _commit_import(client, world.admin_auth, rows)
    assert (job["status"], job["created"], job["merged"], job["error"]) == ("done", 1, 1, None)

    async with db_session() as db:
        people = {
            p.first_name: p
            for p in (await db.execute(select(Person).where(Person.org_id == world.org.id)))
            .scalars()
        }
    assert (people["Ned"].interaction_count, people["Ned"].last_contacted_at) == (1, sent_at)
    assert (people["Kay"].interaction_count, people["Kay"].last_contacted_at) == (1, sent_at)


async def test_import_resumed_after_a_restart_still_recomputes_everyone(world, db_session):
    """The ids from before the restart are gone with the old process; the
    resumed job falls back to the whole org rather than skipping them."""
    sent_at = utcnow() - timedelta(days=4)
    await _stored_email(world.org, "before@customer.example", sent_at)
    await _stored_email(world.org, "after@customer.example", sent_at)
    # Row 0 was committed by the process that died.
    before = await make_person(world.org, first_name="Bea", work_email="before@customer.example")
    rows = [
        {"action": "create", "data": {"first_name": "Bea", "work_email": "before@customer.example"}},
        {"action": "create", "data": {"first_name": "Art", "work_email": "after@customer.example"}},
    ]
    job = await add(
        ImportJob(
            org_id=world.org.id, user_id=world.admin.id, import_type="people",
            status="running", payload=rows, total=2, processed=1, created_count=1,
        )
    )

    await run_import_job(job.id)

    async with db_session() as db:
        assert (await db.get(ImportJob, job.id)).status == "done"
        art = (
            await db.execute(select(Person).where(Person.first_name == "Art"))
        ).scalar_one()
    assert art.interaction_count == 1
    assert (await _person(db_session, before.id)).interaction_count == 1


# --- the imported date -----------------------------------------------------------


async def test_imported_last_contacted_date_survives_the_sync(client, world, db_session):
    imported = datetime(2024, 1, 15, tzinfo=timezone.utc)
    rows = await _preview(
        client,
        world.admin_auth,
        b"First Name,Work Email,Last Contacted\nCora,cora@copper.example,01/15/2024\n",
    )
    job = await _commit_import(client, world.admin_auth, rows)
    assert (job["status"], job["created"]) == ("done", 1)
    async with db_session() as db:
        cora = (await db.execute(select(Person).where(Person.first_name == "Cora"))).scalar_one()
    # Seeded exactly as before — and now also kept where a sync can't reach it.
    assert cora.last_contacted_at == imported
    assert cora.last_contacted_imported_at == imported
    assert cora.interaction_count == 0

    # The sync finds an OLDER email: it counts, but the imported date is later.
    older = await _stored_email(
        world.org, "cora@copper.example", datetime(2023, 6, 1, tzinfo=timezone.utc)
    )
    await _recompute(db_session, world.org, cora.id)
    after = await _person(db_session, cora.id)
    assert (after.interaction_count, after.last_contacted_at) == (1, imported)

    # A NEWER email wins.
    newer_at = datetime(2025, 3, 2, 9, 30, tzinfo=timezone.utc)
    newer = await _stored_email(world.org, "cora@copper.example", newer_at)
    await _recompute(db_session, world.org, cora.id)
    after = await _person(db_session, cora.id)
    assert (after.interaction_count, after.last_contacted_at) == (2, newer_at)

    # The mail goes away (mailbox purge): back to the imported date, not NULL.
    async with db_session() as db:
        await db.execute(delete(EmailMessage).where(EmailMessage.id.in_([older.id, newer.id])))
        await db.commit()
    await _recompute(db_session, world.org, cora.id)
    after = await _person(db_session, cora.id)
    assert (after.interaction_count, after.last_contacted_at) == (0, imported)
    assert after.last_contacted_imported_at == imported

    # The API shape is unchanged: the date is still a nullable ISO timestamp.
    resp = await client.get(f"/api/v1/people/{cora.id}", headers=world.member_auth)
    assert datetime.fromisoformat(resp.json()["last_contacted_at"]) == imported
    assert resp.json()["interaction_count"] == 0


async def test_date_is_cleared_only_when_nothing_derived_or_imported_remains(world, db_session):
    had_events = await make_person(
        world.org, first_name="Gone", interaction_count=3, last_contacted_at=utcnow()
    )
    typed_by_hand = await make_person(
        world.org, first_name="Manual", last_contacted_at=utcnow() - timedelta(days=30)
    )
    await _recompute(db_session, world.org, had_events.id, typed_by_hand.id)
    # Counted events that no longer exist, and no imported date to fall back on.
    gone = await _person(db_session, had_events.id)
    assert (gone.interaction_count, gone.last_contacted_at) == (0, None)
    # Never had events: the date someone typed is the only history there is.
    manual = await _person(db_session, typed_by_hand.id)
    assert manual.last_contacted_at == typed_by_hand.last_contacted_at


# --- the migration's backfill ----------------------------------------------------


async def test_migration_backfills_only_dates_no_sync_has_touched():
    url = make_url(TEST_DATABASE_URL)
    name = f"{url.database}_backfill"
    scratch = url.set(database=name).render_as_string(hide_password=False)
    org_id, imported_id, synced_id, blank_id = (uuid.uuid4() for _ in range(4))
    imported_at = datetime(2024, 1, 15, tzinfo=timezone.utc)
    synced_at = datetime(2026, 9, 1, tzinfo=timezone.utc)

    async def connect(database: str):
        return await asyncpg.connect(
            host=url.host, port=url.port or 5432, user=url.username,
            password=url.password, database=database,
        )

    async def admin(statement: str) -> None:
        conn = await connect("postgres")
        try:
            await conn.execute(statement)
        finally:
            await conn.close()

    async def seed() -> None:
        conn = await connect(name)
        try:
            now = utcnow()
            await conn.execute(
                "INSERT INTO orgs (id, name, created_at) VALUES ($1, 'Acme', $2)", org_id, now
            )
            for pid, count, date in (
                (imported_id, 0, imported_at),  # intact imported date
                (synced_id, 4, synced_at),  # already sync-derived
                (blank_id, 0, None),
            ):
                await conn.execute(
                    "INSERT INTO people (id, org_id, first_name, interaction_count,"
                    " last_contacted_at, created_at, updated_at)"
                    " VALUES ($1, $2, 'P', $3, $4, $5, $5)",
                    pid, org_id, count, date, now,
                )
        finally:
            await conn.close()

    async def columns() -> dict:
        conn = await connect(name)
        try:
            rows = await conn.fetch(
                "SELECT id, last_contacted_at, last_contacted_imported_at, updated_at,"
                " created_at FROM people"
            )
            return {r["id"]: r for r in rows}
        finally:
            await conn.close()

    await admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await admin(f'CREATE DATABASE "{name}"')
    try:
        result = await asyncio.to_thread(
            run_alembic, "upgrade", PREVIOUS_HEAD, database_url=scratch
        )
        assert result.returncode == 0, result.stderr
        await seed()

        result = await asyncio.to_thread(run_alembic, "upgrade", "head", database_url=scratch)
        assert result.returncode == 0, result.stderr
        rows = (await columns())
        assert rows[imported_id]["last_contacted_imported_at"] == imported_at
        assert rows[synced_id]["last_contacted_imported_at"] is None
        assert rows[blank_id]["last_contacted_imported_at"] is None
        # Nothing the user sees moved.
        assert rows[imported_id]["last_contacted_at"] == imported_at
        assert rows[synced_id]["last_contacted_at"] == synced_at
        assert all(r["updated_at"] == r["created_at"] for r in rows.values())

        for step in (("downgrade", "-1"), ("upgrade", "head")):
            result = await asyncio.to_thread(run_alembic, *step, database_url=scratch)
            assert result.returncode == 0, result.stderr
        assert (await columns())[imported_id]["last_contacted_imported_at"] == imported_at
    finally:
        await admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
