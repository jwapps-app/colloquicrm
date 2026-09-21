"""Deactivation cuts every delivery route; chat-account links are admin-only."""

import uuid

import pytest
from sqlalchemy import select

from app.models import ColloquiIntegration, DeviceToken, User
from app.models import Session as DbSession
from app.services import push
from app.services.colloqui import ColloquiClient

from .conftest import add

SPACE_ID = uuid.uuid4()
REMOTE_ID = uuid.uuid4()


@pytest.fixture
def chat(monkeypatch):
    """A fake chat server: records calls, serves one remote account."""

    class Chat:
        removed: list = []
        added: list = []
        remote_users = [{"id": str(REMOTE_ID), "username": "real.name", "display_name": "Real"}]

    chat = Chat()
    chat.removed, chat.added = [], []

    async def users(self):
        return chat.remote_users

    async def add_space_member(self, space_id, user_id, role="member"):
        chat.added.append((space_id, user_id))

    async def remove_space_member(self, space_id, user_id):
        chat.removed.append((space_id, user_id))

    monkeypatch.setattr(ColloquiClient, "users", users)
    monkeypatch.setattr(ColloquiClient, "add_space_member", add_space_member)
    monkeypatch.setattr(ColloquiClient, "remove_space_member", remove_space_member)
    return chat


async def _connect_chat(org):
    await add(
        ColloquiIntegration(
            org_id=org.id,
            base_url="http://chat.invalid",
            api_key="colq_test",
            space_id=SPACE_ID,
            tasks_channel_id=uuid.uuid4(),
        )
    )


async def test_deactivation_removes_devices_chat_link_and_sessions(
    client, world, db_session, chat
):
    await _connect_chat(world.org)
    async with db_session() as db:
        member = await db.get(User, world.member.id)
        member.colloqui_user_id = REMOTE_ID
        member.colloqui_username = "real.name"
        member.notify_channel = "crm_push"
        db.add(DeviceToken(org_id=world.org.id, user_id=member.id, token="a" * 64))
        await db.commit()

    resp = await client.patch(
        f"/api/v1/users/{world.member.id}", headers=world.admin_auth, json={"is_active": False}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is False
    assert resp.json()["colloqui_username"] is None

    async with db_session() as db:
        member = await db.get(User, world.member.id)
        assert member.colloqui_user_id is None and member.colloqui_username is None
        tokens = (
            await db.execute(select(DeviceToken).where(DeviceToken.user_id == member.id))
        ).all()
        sessions = (
            await db.execute(select(DbSession).where(DbSession.user_id == member.id))
        ).all()
    assert tokens == []
    assert sessions == []
    assert chat.removed == [(str(SPACE_ID), str(REMOTE_ID))]
    assert (await client.get("/api/v1/auth/me", headers=world.member_auth)).status_code == 401


async def test_push_is_never_attempted_for_an_inactive_user(world, db_session, monkeypatch):
    attempts = []

    async def fake_send_one(db, device, title, body, extra, badge=None):
        attempts.append(device.token)
        return True

    monkeypatch.setattr(push, "_send_one", fake_send_one)
    assert push.is_configured()
    async with db_session() as db:
        # A device row that outlived (or re-registered after) the cleanup.
        member = await db.get(User, world.member.id)
        member.is_active = False
        db.add(DeviceToken(org_id=world.org.id, user_id=member.id, token="b" * 64))
        db.add(DeviceToken(org_id=world.org.id, user_id=world.admin.id, token="c" * 64))
        await db.commit()

        assert await push.send_to_user(db, world.member.id, "Task due", "Call back") == 0
        assert attempts == []
        # Control: the same call reaches an active user's device.
        assert await push.send_to_user(db, world.admin.id, "Task due", "Call back") == 1
        assert attempts == ["c" * 64]


async def test_member_cannot_link_a_chat_account(client, world, db_session, chat):
    await _connect_chat(world.org)
    resp = await client.post(
        "/api/v1/integrations/colloqui/link",
        headers=world.member_auth,
        json={"colloqui_user_id": str(REMOTE_ID)},
    )
    assert resp.status_code == 403
    async with db_session() as db:
        assert (await db.get(User, world.member.id)).colloqui_user_id is None
    assert chat.added == []


async def test_admin_links_another_user_with_the_servers_username(
    client, world, db_session, chat
):
    await _connect_chat(world.org)
    resp = await client.post(
        "/api/v1/integrations/colloqui/link",
        headers=world.admin_auth,
        json={
            "user_id": str(world.member.id),
            "colloqui_user_id": str(REMOTE_ID),
            "colloqui_username": "someone.else",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "colloqui_user_id": str(REMOTE_ID),
        "colloqui_username": "real.name",
    }
    async with db_session() as db:
        member = await db.get(User, world.member.id)
        admin = await db.get(User, world.admin.id)
    assert (member.colloqui_user_id, member.colloqui_username) == (REMOTE_ID, "real.name")
    assert admin.colloqui_user_id is None
    assert chat.added == [(str(SPACE_ID), str(REMOTE_ID))]


async def test_admin_cannot_link_a_user_from_another_org(client, world, other_world, chat):
    await _connect_chat(world.org)
    resp = await client.post(
        "/api/v1/integrations/colloqui/link",
        headers=world.admin_auth,
        json={"user_id": str(other_world.member.id), "colloqui_user_id": str(REMOTE_ID)},
    )
    assert resp.status_code == 404
