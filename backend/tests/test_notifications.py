"""Where a notification lands: app push, a chat DM, or the team's #tasks
channel — for task assignments, due reminders and automation notices.

Chat and the push relay are faked at their edges; `outbox` records every
message that would have left the building, in order."""

import uuid
from datetime import timedelta

import pytest

from app.models import AutomationRule, ColloquiIntegration, Task, utcnow
from app.services import automations, push
from app.services import notifications as notify
from app.services.colloqui import ColloquiClient, ColloquiError

from .conftest import add, make_person, make_user

CHANNEL = uuid.uuid4()


class Outbox:
    def __init__(self):
        self.sent: list[tuple] = []
        self.devices: set = set()  # user ids with a working device
        self.dm_broken = False

    @property
    def kinds(self) -> list[str]:
        return [m[0] for m in self.sent]


@pytest.fixture
def outbox(monkeypatch):
    box = Outbox()

    async def send_to_user(db, user_id, title, body, extra=None, badge=None):
        if user_id not in box.devices:
            return 0
        box.sent.append(("push", user_id, title, body))
        return 1

    async def open_dm(self, user_id):
        if box.dm_broken:
            raise ColloquiError("no such user")
        return {"id": f"dm:{user_id}"}

    async def send_message(self, channel_id, content):
        kind = "dm" if str(channel_id).startswith("dm:") else "channel"
        box.sent.append((kind, str(channel_id), content))
        return {}

    monkeypatch.setattr(push, "send_to_user", send_to_user)
    monkeypatch.setattr(ColloquiClient, "open_dm", open_dm)
    monkeypatch.setattr(ColloquiClient, "send_message", send_message)
    return box


async def _chat_on(org):
    await add(
        ColloquiIntegration(
            org_id=org.id, base_url="http://chat.invalid", api_key="colq_test",
            space_id=uuid.uuid4(), tasks_channel_id=CHANNEL,
        )
    )


async def _assignee(world, *, channel="colloqui_chat", linked=True, **fields):
    if linked:
        fields.setdefault("colloqui_user_id", uuid.uuid4())
        fields.setdefault("colloqui_username", "sam")
    return await make_user(world.org, display_name="Sam Sales", notify_channel=channel, **fields)


async def _task(world, assignee, **fields) -> Task:
    return await add(
        Task(
            org_id=world.org.id, name="Send the proposal",
            assignee_id=assignee.id if assignee else None, **fields,
        )
    )


async def _assigned(world, task, assignee, actor=None):
    await notify.notify_task_event(
        task.id, "assigned", actor_id=(actor or world.admin).id, assignee_id=assignee.id
    )


# ---- assignment ----


async def test_assignment_goes_to_app_push_when_that_is_the_users_channel(world, outbox):
    sam = await _assignee(world, channel="crm_push")
    outbox.devices.add(sam.id)
    await _chat_on(world.org)
    await _assigned(world, await _task(world, sam), sam)
    assert outbox.sent == [("push", sam.id, "Task assigned to you", "Send the proposal")]


async def test_push_user_without_a_device_gets_a_dm_instead(world, outbox):
    sam = await _assignee(world, channel="crm_push")
    await _chat_on(world.org)
    await _assigned(world, await _task(world, sam), sam)
    assert outbox.kinds == ["dm"]
    assert outbox.sent[0][1] == f"dm:{sam.colloqui_user_id}"
    assert "Task assigned to you: **Send the proposal**" in outbox.sent[0][2]


async def test_a_broken_or_missing_dm_link_falls_back_to_the_tasks_channel(world, outbox):
    await _chat_on(world.org)
    sam = await _assignee(world)
    outbox.dm_broken = True
    await _assigned(world, await _task(world, sam), sam)
    unlinked = await _assignee(world, linked=False)
    await _assigned(world, await _task(world, unlinked), unlinked)

    assert outbox.kinds == ["channel", "channel"]
    assert outbox.sent[0][1] == str(CHANNEL)
    assert "Task reassigned to @sam: **Send the proposal**" in outbox.sent[0][2]
    assert "Task reassigned to Sam Sales:" in outbox.sent[1][2]


async def test_chat_user_on_an_install_without_chat_is_reached_by_push(world, outbox):
    # Same rule as the due reminder: with no chat to deliver to, a registered
    # device beats nowhere, whatever channel the user picked.
    sam = await _assignee(world)
    outbox.devices.add(sam.id)
    await _assigned(world, await _task(world, sam), sam)
    assert outbox.sent == [("push", sam.id, "Task assigned to you", "Send the proposal")]

    nobody_home = await _assignee(world)
    await _assigned(world, await _task(world, nobody_home), nobody_home)
    assert len(outbox.sent) == 1


async def test_nobody_is_told_about_their_own_assignment_or_a_departed_users(world, outbox):
    await _chat_on(world.org)
    sam = await _assignee(world)
    await _assigned(world, await _task(world, sam), sam, actor=sam)
    gone = await _assignee(world, active=False)
    await _assigned(world, await _task(world, gone), gone)
    assert outbox.sent == []


# ---- creation and completion: the team channel ----


async def test_new_task_posts_to_the_channel_and_pushes_without_a_double_ping(world, outbox):
    await _chat_on(world.org)
    chatty = await _assignee(world)
    pushy = await _assignee(world, channel="crm_push", colloqui_username="pat")
    outbox.devices.add(pushy.id)

    for who in (chatty, pushy):
        task = await _task(world, who)
        await notify.notify_task_event(
            task.id, "created", actor_id=world.admin.id, assignee_id=who.id
        )

    assert outbox.kinds == ["channel", "channel", "push"]
    assert "New task for @sam: **Send the proposal**" in outbox.sent[0][2]
    # The push already reached them; the channel post names them plainly.
    assert "New task for Sam Sales: **Send the proposal**" in outbox.sent[1][2]
    assert outbox.sent[2][:3] == ("push", pushy.id, "New task assigned")


async def test_finishing_someone_elses_task_is_announced(world, outbox):
    await _chat_on(world.org)
    sam = await _assignee(world)
    task = await _task(world, sam)
    await notify.notify_task_event(task.id, "completed", actor_id=sam.id, assignee_id=sam.id)
    await notify.notify_task_event(
        task.id, "completed", actor_id=world.admin.id, assignee_id=sam.id
    )
    assert outbox.kinds == ["channel"]
    assert "@sam — task done: **Send the proposal**" in outbox.sent[0][2]


# ---- due reminders ----


async def _due(world, assignee, **fields) -> Task:
    return await _task(world, assignee, due_at=utcnow() - timedelta(minutes=1), **fields)


async def _notified(db_session, task) -> bool:
    async with db_session() as db:
        return (await db.get(Task, task.id)).due_notified_at is not None


async def test_due_reminder_follows_the_same_route(world, outbox, db_session):
    await _chat_on(world.org)
    pushy = await _assignee(world, channel="crm_push")
    outbox.devices.add(pushy.id)
    chatty = await _assignee(world)
    first, second, third = (
        await _due(world, pushy), await _due(world, chatty), await _due(world, None)
    )

    await notify.run_due_pass()

    by_kind = {m[0]: m for m in outbox.sent}
    assert sorted(outbox.kinds) == ["channel", "dm", "push"]
    assert by_kind["push"][1:3] == (pushy.id, "Task reminder")
    assert "Reminder: **Send the proposal**" in by_kind["dm"][2]
    assert "unassigned — reminder: **Send the proposal**" in by_kind["channel"][2]
    for task in (first, second, third):
        assert await _notified(db_session, task)

    await notify.run_due_pass()  # each reminder fires once
    assert len(outbox.sent) == 3


async def test_due_reminder_without_chat_uses_a_device_if_there_is_one(world, outbox, db_session):
    with_device = await _assignee(world)  # prefers chat, but there is none
    outbox.devices.add(with_device.id)
    without = await _assignee(world)
    reachable, unreachable = await _due(world, with_device), await _due(world, without)

    await notify.run_due_pass()

    assert [m[:3] for m in outbox.sent] == [("push", with_device.id, "Task reminder")]
    assert outbox.sent[0][3].startswith("Send the proposal — due ")
    # Nowhere to ever deliver the second one: stamped so it stops retrying.
    assert await _notified(db_session, reachable)
    assert await _notified(db_session, unreachable)


# ---- automation notices ----


async def _notice(world, db_session, recipient) -> dict:
    person = await make_person(world.org, owner_id=recipient.id)
    rule = await add(
        AutomationRule(
            org_id=world.org.id, name="Stale", entity_type="person",
            trigger_type="stale_record", trigger_config={"days": 30},
            action_type="notify", action_config={"message": "{name} needs attention"},
            created_by=world.admin.id,
        )
    )
    async with db_session() as db:
        return await automations._act_notify(db, rule, person, "Pat Person")


async def test_automation_notice_follows_the_same_route(world, outbox, db_session):
    await _chat_on(world.org)
    pushy = await _assignee(world, channel="crm_push")
    outbox.devices.add(pushy.id)
    chatty = await _assignee(world)
    unlinked = await _assignee(world, linked=False)

    delivered = [
        (await _notice(world, db_session, who))["delivered"] for who in (pushy, chatty, unlinked)
    ]

    assert delivered == ["push", "dm", "channel"]
    assert outbox.kinds == ["push", "dm", "channel"]
    assert outbox.sent[0][2:] == ("Automation", "Pat Person needs attention")
    assert "Pat Person needs attention" in outbox.sent[1][2]
    assert "Sam Sales — Pat Person needs attention" in outbox.sent[2][2]


async def test_automation_notice_on_an_install_without_chat(world, outbox, db_session):
    sam = await _assignee(world)
    outbox.devices.add(sam.id)
    assert (await _notice(world, db_session, sam))["delivered"] == "push"
    assert outbox.sent == [("push", sam.id, "Automation", "Pat Person needs attention")]

    nobody_home = await _assignee(world)
    assert (await _notice(world, db_session, nobody_home))["delivered"] is None
    assert len(outbox.sent) == 1
