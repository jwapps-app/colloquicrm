"""Deletes that rely on the database's foreign keys to clean up after them.

The application doesn't delete these child rows itself — ON DELETE CASCADE /
SET NULL does. These tests are what notice if a migration ever drops one."""

import uuid
from datetime import timedelta

from sqlalchemy import func, select

from app.models import (
    AutomationFire,
    AutomationRule,
    CalendarEvent,
    CalendarEventAttendee,
    CustomField,
    CustomFieldValue,
    Note,
    Person,
    PhoneEvent,
    utcnow,
)
from app.services.google.calendar import _delete_calendar_events
from app.services.maintenance import purge_expired_trash

from .conftest import add, make_person


async def _count(db_session, model) -> int:
    async with db_session() as db:
        return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def test_deleting_a_custom_field_removes_its_values(client, world, db_session):
    person = await make_person(world.org)
    field = await add(CustomField(org_id=world.org.id, entity_type="person", name="Nickname"))
    await add(
        CustomFieldValue(field_id=field.id, entity_type="person", entity_id=person.id, value="P")
    )
    resp = await client.delete(f"/api/v1/custom-fields/{field.id}", headers=world.admin_auth)
    assert resp.status_code == 204, resp.text
    assert await _count(db_session, CustomFieldValue) == 0


async def test_deleting_an_automation_rule_removes_its_fire_log(client, world, db_session):
    person = await make_person(world.org)
    rule = await add(
        AutomationRule(
            org_id=world.org.id, name="Stale", entity_type="person",
            trigger_type="stale_record", trigger_config={"days": 30},
            action_type="add_tag", action_config={"tag": "stale"},
        )
    )
    await add(
        AutomationFire(
            org_id=world.org.id, rule_id=rule.id, entity_type="person", entity_id=person.id
        )
    )
    resp = await client.delete(f"/api/v1/automations/{rule.id}", headers=world.admin_auth)
    assert resp.status_code == 204, resp.text
    assert await _count(db_session, AutomationFire) == 0


async def test_deleting_calendar_events_removes_their_attendees(world, db_session):
    event = await add(
        CalendarEvent(org_id=world.org.id, google_event_id="ev1", starts_at=utcnow())
    )
    await add(CalendarEventAttendee(event_id=event.id, email="pat@example.com"))
    async with db_session() as db:
        await _delete_calendar_events(db, [event.id])
        await db.commit()
    assert await _count(db_session, CalendarEventAttendee) == 0


async def test_purging_a_record_unlinks_notes_elsewhere_from_its_calls(world, db_session):
    doomed = await make_person(world.org, deleted_at=utcnow() - timedelta(days=400))
    other = await make_person(world.org)
    call = await add(
        PhoneEvent(
            org_id=world.org.id, rc_id=f"manual:{uuid.uuid4().hex}", kind="call",
            direction="outbound", other_number="", entity_type="person", entity_id=doomed.id,
        )
    )
    note = await add(
        Note(
            org_id=world.org.id, entity_type="person", entity_id=other.id,
            body="About that call", phone_event_id=call.id,
        )
    )

    assert await purge_expired_trash() == 1

    async with db_session() as db:
        assert await db.get(Person, doomed.id) is None
        assert await db.get(PhoneEvent, call.id) is None
        kept = await db.get(Note, note.id)
        assert kept is not None and kept.phone_event_id is None
