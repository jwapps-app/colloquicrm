"""Google sync against a fake Google: isolation between accounts, the message
retry queue, calendar tombstones, all-day events, and no network round-trip
inside a database transaction."""

import base64
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import (
    CalendarEvent,
    EmailMessage,
    GmailSyncFailure,
    GoogleAccount,
    GoogleIntegration,
    utcnow,
)
from app.services import google

from .conftest import add, make_person, make_user


class FakeGoogle:
    """Answers the Calendar and Gmail endpoints the sync uses, keyed by the
    caller's access token. Every request also checks that no sync session is
    sitting in a transaction while the "network" is in flight."""

    def __init__(self):
        self.events: dict[str, list[dict]] = {}  # token -> calendar items
        self.calendar_down: set[str] = set()  # tokens whose calendar 500s
        self.history: dict[str, list[str]] = {}  # token -> new message ids
        self.messages: dict[str, dict] = {}  # gmail id -> message resource
        self.broken_messages: set[str] = set()  # gmail ids that 500
        self.requests: list[str] = []
        self.in_transaction_during_request: list[str] = []
        self.sessions: list = []

    def session_factory(self):
        session = SessionLocal()
        self.sessions.append(session)
        return session

    def fetches_of(self, gmail_id: str) -> int:
        return sum(1 for path in self.requests if path.endswith(f"/messages/{gmail_id}"))

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        if any(s.in_transaction() for s in self.sessions):
            self.in_transaction_during_request.append(path)
        token = request.headers.get("authorization", "").removeprefix("Bearer ")

        if path.endswith("/calendars/primary/events"):
            if token in self.calendar_down:
                return httpx.Response(500, json={"error": {"message": "backend error"}})
            return httpx.Response(200, json={"items": self.events.get(token, [])})
        if path.endswith("/users/me/history"):
            ids = self.history.pop(token, [])
            return httpx.Response(
                200,
                json={
                    "history": [{"messagesAdded": [{"message": {"id": i}} for i in ids]}],
                    "historyId": "200",
                },
            )
        match = re.search(r"/users/me/messages/([^/]+)$", path)
        if match:
            gmail_id = match.group(1)
            if gmail_id in self.broken_messages:
                return httpx.Response(500, json={"error": {"message": "backend error"}})
            if gmail_id not in self.messages:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            return httpx.Response(200, json=self.messages[gmail_id])
        return httpx.Response(404, json={"error": {"message": f"unexpected {path}"}})


@pytest.fixture
async def fake_google(monkeypatch):
    fake = FakeGoogle()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    monkeypatch.setattr(google, "_get_client", lambda: client)
    monkeypatch.setattr(google, "SessionLocal", fake.session_factory)
    yield fake
    await client.aclose()


async def _connect(world, user, token: str, *, connected_at=None) -> GoogleAccount:
    return await add(
        GoogleAccount(
            user_id=user.id,
            org_id=world.org.id,
            email=user.email,
            refresh_token="refresh-" + token,
            access_token=token,
            access_expires_at=utcnow() + timedelta(hours=1),
            scopes=" ".join(google.SCOPES),
            connected_at=connected_at or utcnow(),
            # Backfill already finished: passes use the incremental feed.
            gmail_backfill_done=True,
            gmail_history_id="100",
        )
    )


def _event(event_id: str, start: datetime, **extra) -> dict:
    return {
        "id": event_id,
        "status": "confirmed",
        "summary": event_id,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
        **extra,
    }


def _message(gmail_id: str, sender: str, to: str) -> dict:
    body = base64.urlsafe_b64encode(b"Hello there").decode().rstrip("=")
    return {
        "id": gmail_id,
        "threadId": "t-" + gmail_id,
        "labelIds": ["INBOX"],
        "snippet": "Hello there",
        "internalDate": str(int(utcnow().timestamp() * 1000)),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": to},
                {"name": "Subject", "value": "Hi " + gmail_id},
                {"name": "Message-ID", "value": f"<{gmail_id}@mail.example>"},
            ],
            "body": {"data": body},
        },
    }


@pytest.fixture
async def google_world(world):
    await add(GoogleIntegration(org_id=world.org.id, client_id="cid", client_secret="secret"))
    return world


async def _account(user_id) -> GoogleAccount:
    async with SessionLocal() as db:
        return await db.get(GoogleAccount, user_id)


async def _event_ids() -> set[str]:
    async with SessionLocal() as db:
        return set((await db.execute(select(CalendarEvent.google_event_id))).scalars())


async def test_first_account_failing_does_not_stop_the_second(google_world, fake_google):
    world = google_world
    await _connect(world, world.admin, "tok-admin", connected_at=utcnow() - timedelta(days=2))
    await _connect(world, world.member, "tok-member", connected_at=utcnow() - timedelta(days=1))
    fake_google.calendar_down.add("tok-admin")
    fake_google.events["tok-member"] = [_event("member-meeting", utcnow() + timedelta(days=1))]

    await google.run_sync_pass()  # must not raise

    assert await _event_ids() == {"member-meeting"}
    failed, healthy = await _account(world.admin.id), await _account(world.member.id)
    assert failed.sync_error and "500" in failed.sync_error
    assert failed.last_synced_at is None
    assert healthy.sync_error is None
    assert healthy.last_synced_at is not None


async def test_deactivated_users_mailbox_is_not_synced(google_world, fake_google):
    world = google_world
    leaver = await make_user(world.org, "leaver@example.com", active=False)
    await _connect(world, leaver, "tok-leaver")
    await google.run_sync_pass()
    assert fake_google.requests == []


async def test_unfetchable_message_is_queued_then_stored_when_it_recovers(
    google_world, fake_google
):
    world = google_world
    await make_person(world.org, work_email="client@customer.example")
    await _connect(world, world.member, "tok")
    for gmail_id in ("m1", "m2"):
        fake_google.messages[gmail_id] = _message(
            gmail_id, "Client <client@customer.example>", world.member.email
        )
    fake_google.broken_messages.add("m2")
    fake_google.history["tok"] = ["m1", "m2"]

    await google.run_sync_pass()
    async with SessionLocal() as db:
        stored = set((await db.execute(select(EmailMessage.gmail_id))).scalars())
        queue = (await db.execute(select(GmailSyncFailure))).scalars().all()
    assert stored == {"m1"}
    assert [(f.kind, f.key, f.attempts, f.given_up) for f in queue] == [("message", "m2", 1, False)]
    # The feed position moved on — the queue is what remembers m2.
    assert (await _account(world.member.id)).gmail_history_id == "200"

    fake_google.broken_messages.clear()
    await google.run_sync_pass()
    async with SessionLocal() as db:
        stored = set((await db.execute(select(EmailMessage.gmail_id))).scalars())
        queue = (await db.execute(select(GmailSyncFailure))).scalars().all()
    assert stored == {"m1", "m2"}
    assert queue == []
    assert (await _account(world.member.id)).sync_error is None


async def test_message_that_never_fetches_is_given_up_after_five_attempts(
    google_world, fake_google
):
    world = google_world
    await make_person(world.org, work_email="client@customer.example")
    await _connect(world, world.member, "tok")
    fake_google.broken_messages.add("cursed")
    fake_google.history["tok"] = ["cursed"]

    for _ in range(google.MAX_FAILURE_ATTEMPTS + 2):
        await google.run_sync_pass()

    async with SessionLocal() as db:
        row = (await db.execute(select(GmailSyncFailure))).scalar_one()
    assert (row.key, row.attempts, row.given_up) == ("cursed", google.MAX_FAILURE_ATTEMPTS, True)
    assert fake_google.fetches_of("cursed") == google.MAX_FAILURE_ATTEMPTS
    assert (await _account(world.member.id)).sync_error is None


async def test_cancelled_and_vanished_calendar_events_are_removed(google_world, fake_google):
    world = google_world
    await _connect(world, world.member, "tok")
    # Outside the reconciled window, so only its tombstone can remove it.
    long_ago = utcnow() - timedelta(days=200)
    soon = utcnow() + timedelta(days=3)
    fake_google.events["tok"] = [
        _event("cancelled-later", long_ago),
        _event("vanishes-later", soon),
        _event("stays", soon + timedelta(days=1)),
    ]
    await google.run_sync_pass()
    assert await _event_ids() == {"cancelled-later", "vanishes-later", "stays"}

    fake_google.events["tok"] = [
        {"id": "cancelled-later", "status": "cancelled"},
        _event("stays", soon + timedelta(days=1)),
    ]
    await google.run_sync_pass()
    assert await _event_ids() == {"stays"}


async def test_all_day_events_are_stored_at_noon_utc(google_world, fake_google):
    world = google_world
    await _connect(world, world.member, "tok")
    fake_google.events["tok"] = [
        {
            "id": "all-day",
            "status": "confirmed",
            "summary": "Conference",
            "start": {"date": "2026-09-18"},
            "end": {"date": "2026-09-19"},
        }
    ]
    await google.run_sync_pass()
    async with SessionLocal() as db:
        event = (await db.execute(select(CalendarEvent))).scalar_one()
    assert event.all_day is True
    assert event.starts_at == datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    # Noon UTC is still September 18 from UTC-11 through UTC+11.
    for offset in (-11, -5, 0, 11):
        local = event.starts_at.astimezone(timezone(timedelta(hours=offset)))
        assert (local.month, local.day) == (9, 18), offset


async def test_no_google_request_is_made_inside_a_database_transaction(
    google_world, fake_google, monkeypatch
):
    world = google_world
    await make_person(world.org, work_email="client@customer.example")
    # An expired access token adds the refresh round-trip to the pass.
    account = await _connect(world, world.member, "tok")
    async with SessionLocal() as db:
        stored = await db.get(GoogleAccount, account.user_id)
        stored.access_expires_at = utcnow() - timedelta(minutes=5)
        await db.commit()

    async def refresh(url, data):
        assert url == settings.google_token_url
        if any(s.in_transaction() for s in fake_google.sessions):
            fake_google.in_transaction_during_request.append("token refresh")
        fake_google.requests.append("token refresh")
        return {"access_token": "tok", "expires_in": 3600}

    monkeypatch.setattr(google, "_post_form", refresh)
    fake_google.events["tok"] = [
        _event(f"meeting-{i}", utcnow() + timedelta(days=i + 1)) for i in range(3)
    ]
    for gmail_id in ("a", "b", "c"):
        fake_google.messages[gmail_id] = _message(
            gmail_id, "client@customer.example", world.member.email
        )
    fake_google.history["tok"] = ["a", "b", "c"]
    await google.run_sync_pass()

    assert "token refresh" in fake_google.requests
    assert len(fake_google.requests) >= 6  # refresh, calendar, history, 3 messages
    assert fake_google.in_transaction_during_request == []
    async with SessionLocal() as db:
        assert len((await db.execute(select(EmailMessage))).all()) == 3
    assert (await _account(world.member.id)).sync_error is None
