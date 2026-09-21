"""The relay caller: what counts as a dead device, and what doesn't.

A fake relay answers /notify; the tests watch which device rows survive."""

import json

import httpx
import pytest
from sqlalchemy import select

from app.models import DeviceToken
from app.services import push

from .conftest import add


class FakeRelay:
    def __init__(self):
        self.replies: list[httpx.Response] = []
        self.requests: list[dict] = []

    def queue(self, status: int, body, headers: dict | None = None) -> None:
        self.replies.append(httpx.Response(status, json=body, headers=headers or {}))

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        return self.replies.pop(0)


@pytest.fixture
async def relay(monkeypatch):
    fake = FakeRelay()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    monkeypatch.setattr(push, "_get_client", lambda: client)
    yield fake
    await client.aclose()


async def _device(world, environment: str = "production") -> DeviceToken:
    return await add(
        DeviceToken(
            org_id=world.org.id, user_id=world.admin.id, token="a" * 64, environment=environment
        )
    )


async def _send(db_session, world, title="Task due", body="Call back") -> int:
    async with db_session() as db:
        return await push.send_to_user(db, world.admin.id, title, body, {"task_id": "t1"})


async def _stored(db_session) -> list[DeviceToken]:
    async with db_session() as db:
        return list((await db.execute(select(DeviceToken))).scalars())


async def test_delivered_push_keeps_the_device(world, db_session, relay):
    await _device(world)
    relay.queue(200, {"status": "sent", "response_ms": 12})
    assert await _send(db_session, world) == 1
    assert len(await _stored(db_session)) == 1
    assert relay.requests[0]["sandbox"] is False


@pytest.mark.parametrize("reason", ["Unregistered", "DeviceTokenNotForTopic"])
async def test_token_apple_says_is_gone_is_pruned(world, db_session, relay, reason):
    await _device(world)
    relay.queue(410, {"detail": reason, "reason": reason})
    assert await _send(db_session, world) == 0
    assert await _stored(db_session) == []
    assert len(relay.requests) == 1


async def test_bad_token_is_retried_in_the_other_environment_before_deleting(
    world, db_session, relay
):
    # A TestFlight token recorded as sandbox: Apple calls it a bad token, but
    # it is fine — it just belongs to production.
    await _device(world, environment="sandbox")
    relay.queue(410, {"detail": "BadDeviceToken", "reason": "BadDeviceToken"})
    relay.queue(200, {"status": "sent", "response_ms": 9})
    assert await _send(db_session, world) == 1
    assert [r["sandbox"] for r in relay.requests] == [True, False]
    (kept,) = await _stored(db_session)
    assert kept.environment == "production"


async def test_token_bad_in_both_environments_is_pruned(world, db_session, relay):
    await _device(world)
    relay.queue(410, {"detail": "BadDeviceToken", "reason": "BadDeviceToken"})
    relay.queue(410, {"detail": "BadDeviceToken", "reason": "BadDeviceToken"})
    assert await _send(db_session, world) == 0
    assert await _stored(db_session) == []
    assert len(relay.requests) == 2


async def test_bad_token_is_kept_when_the_second_opinion_never_arrives(world, db_session, relay):
    await _device(world)
    relay.queue(410, {"detail": "BadDeviceToken", "reason": "BadDeviceToken"})
    relay.queue(502, {"detail": "APNs delivery failed"})
    assert await _send(db_session, world) == 0
    (kept,) = await _stored(db_session)
    assert kept.environment == "production"


async def test_validation_error_echoing_a_reason_word_never_prunes(world, db_session, relay):
    # A 422's detail repeats our own title back. A task that happens to be
    # called "Unregistered" must not cost the user their device.
    await _device(world)
    relay.queue(422, {"detail": [{"loc": ["body", "title"], "msg": "bad", "input": "Unregistered"}]})
    assert await _send(db_session, world, title="Unregistered") == 0
    assert len(await _stored(db_session)) == 1


@pytest.mark.parametrize(
    "status, body, headers",
    [
        (502, {"detail": "APNs delivery failed"}, None),
        (413, {"detail": "Payload too large for APNs"}, None),
        (429, {"detail": "Too many requests"}, {"Retry-After": "30"}),
        (410, {"detail": "Unregistered"}, None),  # a 410 without the exact reason field
    ],
)
async def test_other_failures_leave_the_device_alone(world, db_session, relay, status, body, headers):
    await _device(world)
    relay.queue(status, body, headers)
    assert await _send(db_session, world) == 0
    assert len(await _stored(db_session)) == 1
