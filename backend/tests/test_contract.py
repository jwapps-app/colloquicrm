"""The response shapes the web app and the iOS app are written against.

Each test pins the exact key set of one response. Adding a key here is a
deliberate, additive change; a test that fails because a key went missing or
was renamed is a breaking change for a shipped client — keep the old key.
A new model column shows up here too: decide whether clients should see it
(add it below) or not (mark the column `info={"private": True}`).
"""

from datetime import timedelta

from app.models import (
    Activity,
    CalendarEvent,
    CalendarEventAttendee,
    EmailMessage,
    EmailParticipant,
    Note,
    PhoneEvent,
    Task,
    utcnow,
)

from .conftest import add, make_company, make_lead, make_opportunity, make_person

API = "/api/v1"

EXTRAS = {"tags", "custom_fields"}
ADDRESS = {"street", "city", "state", "postal_code", "country"}
STAMPS = {"created_at", "updated_at", "deleted_at"}
NAME = {"first_name", "middle_name", "last_name", "prefix", "suffix"}

PERSON = (
    {"id", "title", "details", "company_id", "contact_type", "owner_id"}
    | {"work_email", "personal_email", "work_phone", "mobile_phone"}
    | {"work_website", "personal_website", "linkedin", "facebook"}
    | {"last_contacted_at", "last_contacted_imported_at", "interaction_count"}
    | {"owner_name", "company_name", "has_overdue_task"}
    | NAME | ADDRESS | STAMPS | EXTRAS
)
LEAD = (
    {"id", "title", "details", "value", "currency", "company_name", "source", "status"}
    | {"email", "work_phone", "mobile_phone"}
    | {"work_website", "personal_website", "linkedin", "facebook"}
    | {"owner_id", "converted_at", "converted_person_id"}
    | {"owner_name", "has_overdue_task"}
    | NAME | ADDRESS | STAMPS | EXTRAS
)
COMPANY = (
    {"id", "name", "details", "email_domain", "contact_type", "owner_id"}
    | {"work_phone", "work_website", "linkedin", "facebook", "owner_name"}
    | ADDRESS | STAMPS | EXTRAS
)
OPPORTUNITY = (
    {"id", "name", "details", "company_id", "primary_person_id", "status", "priority"}
    | {"owner_id", "close_date", "value", "currency", "win_probability"}
    | {"pipeline_id", "stage_id", "source", "loss_reason", "closed_at"}
    | {"owner_name", "company_name", "person_name"}
    | STAMPS | EXTRAS
)
TASK = {
    "id", "name", "details", "entity_type", "entity_id", "due_at", "reminder_at",
    "repeat_every", "repeat_unit", "priority", "status", "assignee_id", "created_by",
    "completed_at", "due_notified_at", "created_at", "updated_at",
    "assignee_name", "created_by_name", "entity_label",
}

EMAIL_ROW = {
    "id", "subject", "snippet", "from_email", "from_name", "is_outgoing", "sent_at",
    "gmail_id", "owner_user_id",
}
PHONE_ROW = {
    "id", "kind", "direction", "other_number", "other_name", "happened_at",
    "duration_seconds", "result", "text", "recording_id",
}
NOTE_ROW = {
    "id", "entity_type", "entity_id", "body", "phone_event_id", "author_id", "created_at",
    "author_name",
}
ACTIVITY_ROW = {
    "id", "entity_type", "entity_id", "kind", "payload", "actor_id", "created_at", "actor_name",
}
CALENDAR_ROW = {
    "id", "summary", "location", "starts_at", "ends_at", "all_day", "html_link", "attendees",
}
RELATED = {"entity_type", "entity_id", "label"}

# Feed items: the row, with `type`/`at` standing in for its kind and timestamp.
FEED_EMAIL = (EMAIL_ROW - {"sent_at"}) | {"type", "at", "related"}
FEED_PHONE = (PHONE_ROW - {"kind", "happened_at"}) | {
    "type", "at", "related", "entity_type", "entity_id",
}
FEED_NOTE = {"type", "at", "id", "body", "author_name", "entity_type", "entity_id", "related"}
FEED_ACTIVITY = {
    "type", "at", "id", "kind", "payload", "actor_name", "entity_type", "entity_id", "related",
}
# A record's own timeline adds what its page needs.
RECORD_PHONE = FEED_PHONE | {"notes"}
RECORD_NOTE = FEED_NOTE | {"author_id", "phone_event_id"}


async def _json(client, auth, path, **params):
    resp = await client.get(f"{API}{path}", headers=auth, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _seed_history(world):
    """A person with one of everything on their timeline."""
    person = await make_person(
        world.org, work_email="pat@example.com", mobile_phone="555-123-4567"
    )
    now = utcnow()
    msg = await add(
        EmailMessage(
            org_id=world.org.id, gmail_id="g1", rfc_message_id="<1@example.com>",
            subject="Hello", snippet="Hi", from_email="pat@example.com", from_name="Pat",
            sent_at=now, owner_user_id=world.member.id,
        )
    )
    call = await add(
        PhoneEvent(
            org_id=world.org.id, rc_id="call:1", kind="call", direction="inbound",
            other_number="+15551234567", happened_at=now - timedelta(minutes=1),
            duration_seconds=65, result="Accepted",
        )
    )
    event = await add(
        CalendarEvent(
            org_id=world.org.id, google_event_id="ev1", summary="Lunch",
            starts_at=now, ends_at=now + timedelta(hours=1),
        )
    )
    await add(
        EmailParticipant(email_id=msg.id, email="pat@example.com", kind="from", direct=True),
        CalendarEventAttendee(event_id=event.id, email="pat@example.com", display_name="Pat"),
        Note(
            org_id=world.org.id, entity_type="person", entity_id=person.id, body="Standalone",
            author_id=world.member.id, created_at=now - timedelta(minutes=2),
        ),
        Note(
            org_id=world.org.id, entity_type="person", entity_id=person.id, body="On the call",
            author_id=world.member.id, phone_event_id=call.id, created_at=now - timedelta(minutes=3),
        ),
        Activity(
            org_id=world.org.id, entity_type="person", entity_id=person.id, kind="created",
            actor_id=world.admin.id, payload={}, created_at=now - timedelta(minutes=4),
        ),
    )
    return person


async def test_core_records(client, world):
    company = await make_company(world.org, owner_id=world.member.id)
    person = await make_person(world.org, company_id=company.id, owner_id=world.member.id)
    lead = await make_lead(world.org)
    opp = await make_opportunity(world.org, company_id=company.id, primary_person_id=person.id)
    task = await add(
        Task(org_id=world.org.id, name="Call back", entity_type="person", entity_id=person.id)
    )
    auth = world.member_auth
    for path, record, keys in (
        ("/people", person, PERSON),
        ("/leads", lead, LEAD),
        ("/companies", company, COMPANY),
        ("/opportunities", opp, OPPORTUNITY),
        ("/tasks", task, TASK),
    ):
        one = await _json(client, auth, f"{path}/{record.id}")
        assert set(one) == keys, path
        listing = await _json(client, auth, path)
        assert set(listing) == {"items", "total", "page", "page_size"}, path
        assert set(listing["items"][0]) == keys, path


async def test_feed_items(client, world):
    await _seed_history(world)
    body = await _json(client, world.member_auth, "/feed", page_size=50)
    assert set(body) == {"items", "page", "page_size", "has_more"}
    by_type = {i["type"]: i for i in body["items"]}
    assert set(by_type) == {"email", "call", "note", "activity"}
    assert set(by_type["email"]) == FEED_EMAIL
    assert set(by_type["call"]) == FEED_PHONE
    assert set(by_type["note"]) == FEED_NOTE
    assert set(by_type["activity"]) == FEED_ACTIVITY
    for item in body["items"]:
        for ref in item["related"]:
            assert set(ref) == RELATED


async def test_record_timeline_items(client, world):
    person = await _seed_history(world)
    body = await _json(
        client, world.member_auth, "/timeline", entity_type="person", entity_id=str(person.id)
    )
    assert set(body) == {"items", "page", "page_size", "has_more"}
    by_type = {i["type"]: i for i in body["items"]}
    assert set(by_type["email"]) == FEED_EMAIL
    assert set(by_type["call"]) == RECORD_PHONE
    assert set(by_type["call"]["notes"][0]) == NOTE_ROW
    assert set(by_type["note"]) == RECORD_NOTE
    assert set(by_type["activity"]) == FEED_ACTIVITY


async def test_per_record_lists(client, world):
    person = await _seed_history(world)
    auth = world.member_auth
    target = {"entity_type": "person", "entity_id": str(person.id)}

    emails = await _json(client, auth, "/emails", **target)
    assert set(emails) == {"items"}
    assert set(emails["items"][0]) == EMAIL_ROW

    calls = await _json(client, auth, "/integrations/ringcentral/events", **target)
    assert set(calls) == {"items"}
    assert set(calls["items"][0]) == PHONE_ROW

    notes = await _json(client, auth, "/notes", **target)
    assert set(notes) == {"items"}
    assert set(notes["items"][0]) == NOTE_ROW

    activities = await _json(client, auth, "/activities", **target)
    assert set(activities) == {"items", "total", "page", "page_size"}
    assert set(activities["items"][0]) == ACTIVITY_ROW

    events = await _json(client, auth, "/calendar-events", **target)
    assert set(events) == {"items"}
    assert set(events["items"][0]) == CALENDAR_ROW
    assert set(events["items"][0]["attendees"][0]) == {"email", "display_name"}


async def test_email_search(client, world):
    await _seed_history(world)
    body = await _json(client, world.member_auth, "/emails/search", q="Hello")
    assert set(body) == {"items", "page", "has_more"}
    assert set(body["items"][0]) == EMAIL_ROW | {"related"}
    assert set(body["items"][0]["related"][0]) == RELATED


async def test_created_note_and_task_echo_the_list_shape(client, world):
    person = await make_person(world.org)
    resp = await client.post(
        f"{API}/notes",
        headers=world.member_auth,
        json={"entity_type": "person", "entity_id": str(person.id), "body": "Hi"},
    )
    assert resp.status_code == 201, resp.text
    assert set(resp.json()) == NOTE_ROW
    resp = await client.post(f"{API}/tasks", headers=world.member_auth, json={"name": "Do it"})
    assert resp.status_code == 201, resp.text
    assert set(resp.json()) == TASK

