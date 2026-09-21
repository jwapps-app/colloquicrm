"""RingCentral troubleshooting against a fake RingCentral: the diagnose route
reports what the call log and the CRM hold for a number, and never waits on
the network with a database transaction open."""

from datetime import timedelta

import httpx
import pytest

from app.models import PhoneEvent, RingCentralIntegration, utcnow
from app.services import ringcentral

from .conftest import add, make_person


class FakeRingCentral:
    def __init__(self, sessions: list):
        self.sessions = sessions
        self.requests: list[str] = []
        self.in_transaction_during_request: list[str] = []
        self.call_log: list[dict] = []
        self.down = False

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        if any(s.in_transaction() for s in self.sessions):
            self.in_transaction_during_request.append(path)
        if path.endswith("/restapi/oauth/token"):
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
        if path.endswith("/call-log"):
            if self.down:
                return httpx.Response(503, json={"errors": [{"message": "unavailable"}]})
            return httpx.Response(200, json={"records": self.call_log})
        return httpx.Response(404, json={"errors": [{"message": f"unexpected {path}"}]})


@pytest.fixture
async def fake_rc(monkeypatch, request_sessions):
    fake = FakeRingCentral(request_sessions)
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    monkeypatch.setattr(ringcentral, "_get_client", lambda: client)
    yield fake
    await client.aclose()


async def _connect(world, *, token_expired: bool) -> None:
    expires = utcnow() + (timedelta(minutes=-5) if token_expired else timedelta(hours=1))
    await add(
        RingCentralIntegration(
            org_id=world.org.id, client_id="cid", client_secret="secret", jwt="jwt",
            access_token="tok", access_expires_at=expires, own_numbers=["+15550001111"],
        )
    )


@pytest.mark.parametrize("token_expired", [False, True], ids=["live-token", "expired-token"])
async def test_diagnose_makes_no_ringcentral_request_inside_a_transaction(
    client, world, fake_rc, token_expired
):
    await _connect(world, token_expired=token_expired)
    person = await make_person(world.org, work_phone="(555) 123-4567")
    await add(
        PhoneEvent(
            org_id=world.org.id, rc_id="rc-1", kind="call", direction="inbound",
            other_number="+15551234567", happened_at=utcnow(),
        )
    )
    fake_rc.call_log = [
        {"direction": "Inbound", "startTime": "2026-09-01T10:00:00Z", "duration": 61,
         "result": "Accepted"},
        {"direction": "Outbound", "startTime": "2026-09-02T10:00:00Z", "duration": 12,
         "result": "Call connected"},
    ]

    resp = await client.get(
        "/api/v1/integrations/ringcentral/diagnose",
        headers=world.admin_auth,
        params={"number": "555-123-4567"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["normalized"] == "+15551234567"
    assert body["crm_match"] == {"entity_type": "person", "entity_id": str(person.id)}
    assert (body["ringcentral_call_hits"], body["stored_events"]) == (2, 1)
    assert body["own_numbers"] == ["+15550001111"]

    assert len(fake_rc.requests) == (2 if token_expired else 1)
    assert fake_rc.in_transaction_during_request == []


async def test_diagnose_reports_a_ringcentral_outage_as_a_bad_gateway(client, world, fake_rc):
    await _connect(world, token_expired=False)
    fake_rc.down = True
    resp = await client.get(
        "/api/v1/integrations/ringcentral/diagnose",
        headers=world.admin_auth,
        params={"number": "555-123-4567"},
    )
    assert resp.status_code == 502, resp.text
    assert "503" in resp.json()["detail"]


async def test_synced_call_counts_for_everyone_sharing_the_number(world, db_session):
    # The sync's phone map keeps one owner per number; a shared line still
    # belongs to everyone who lists it, and each of them gets the count.
    from sqlalchemy import select

    from app.models import Person

    await _connect(world, token_expired=False)
    first = await make_person(world.org, first_name="Front", last_name="Desk", work_phone="(555) 123-4567")
    second = await make_person(world.org, first_name="Office", last_name="Manager", mobile_phone="555-123-4567")
    await add(
        PhoneEvent(
            org_id=world.org.id, rc_id="call:shared-1", kind="call", direction="inbound",
            other_number="+15551234567", happened_at=utcnow(),
        )
    )
    async with db_session() as db:
        cfg = (
            await db.execute(
                select(RingCentralIntegration).where(RingCentralIntegration.org_id == world.org.id)
            )
        ).scalar_one()
        # Only `first` made it into the map, exactly as the last-match-wins build leaves it.
        phone_map = {"+15551234567": ("person", first.id)}
        await ringcentral._recompute_touched(db, cfg, phone_map, {"+15551234567"})
        await db.commit()
        counts = dict(
            (await db.execute(select(Person.id, Person.interaction_count))).all()
        )
    assert counts[first.id] == 1
    assert counts[second.id] == 1
