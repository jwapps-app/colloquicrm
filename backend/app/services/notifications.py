"""Telling people things: one route to a user, and the task notifications
that use it.

A user is reached over ONE channel — never two for the same notice:

  1. app push, if that is the channel they chose and a device takes it;
  2. otherwise a chat DM to their linked Colloqui account;
  3. otherwise a post in the team's #tasks channel that names them;
  4. on an install with no chat at all, app push even for a user who
     prefers chat — better their phone than nowhere.

deliver() is that route, and the only place it is written down. Task
assignments, due reminders and automation notices all go through it; a new
channel or a per-user rule (quiet hours, say) is a change to deliver() alone.

Every send is fire-and-forget: chat or the push relay being down must never
break or delay a CRM action.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import func, or_, select

from app.config import settings
from app.db import SessionLocal
from app.models import ColloquiIntegration, DeviceToken, Task, User, utcnow
from app.services import colloqui, push
from app.services.background import spawn
from app.services.colloqui import ColloquiError

log = logging.getLogger("notifications")

REMINDER_INTERVAL_SECONDS = 60

_LOOK_UP = object()


# ---- who, and how to name them ----


async def active_user(db, user_id: uuid.UUID | None) -> User | None:
    """The user a personal notification may go to — None for a deactivated
    account. Former staff must not keep getting task names over push or DM
    just because old tasks still carry their id; callers treat that exactly
    like an unassigned task."""
    if user_id is None:
        return None
    return (
        await db.execute(select(User).where(User.id == user_id, User.is_active.is_(True)))
    ).scalar_one_or_none()


def wants_push(user: User | None) -> bool:
    return user is not None and user.notify_channel == "crm_push"


def mention(user: User | None) -> str:
    """How a channel post names someone: an @mention when their chat account
    is linked (it pings them), their display name otherwise."""
    if user is None:
        return "unassigned"
    if user.colloqui_username:
        return f"@{user.colloqui_username}"
    return user.display_name


async def attention_count(db, user_id: uuid.UUID) -> int:
    """Open tasks needing this user's attention now — overdue or inside the
    default reminder lead. Sent as the app badge with every push."""
    horizon = utcnow() + timedelta(minutes=settings.task_reminder_lead_minutes)
    return (
        await db.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.assignee_id == user_id,
                Task.status == "open",
                Task.due_at <= horizon,
            )
        )
    ).scalar_one()


# ---- the route ----


async def _push(db, user: User, title: str, body: str, data: dict) -> bool:
    return bool(
        await push.send_to_user(
            db, user.id, title, body, data, badge=await attention_count(db, user.id)
        )
    )


async def deliver(
    db,
    org_id: uuid.UUID,
    user: User | None,
    *,
    title: str,
    body: str,
    data: dict,
    dm_text: str,
    channel_text: Callable[[str], str],
    chat: ColloquiIntegration | None = _LOOK_UP,
) -> str | None:
    """Get one notice to one user. Returns how it went out — "push", "dm" or
    "channel" — or None when there was nowhere to send it.

    `title`/`body`/`data` are the push; `dm_text` is the chat DM;
    `channel_text(who)` is the #tasks post, given how to name the user there.
    `user` None (an unassigned task) skips the personal channels and goes to
    #tasks. Pass `chat` when the org's connection row is already in hand.

    A DM that fails (stale or broken link) falls through to #tasks rather
    than stranding the notice; a failing #tasks post raises ColloquiError."""
    prefers_push = wants_push(user)
    if prefers_push and await _push(db, user, title, body, data):
        return "push"
    # No working device — fall through to chat so it isn't lost.
    row = await colloqui.get_integration(db, org_id) if chat is _LOOK_UP else chat
    if not colloqui.is_enabled(row):
        if user is not None and not prefers_push and await _push(db, user, title, body, data):
            return "push"
        return None
    client = colloqui.client_for(row)
    if user is not None and user.colloqui_user_id:
        try:
            dm = await client.open_dm(str(user.colloqui_user_id))
            await client.send_message(dm["id"], dm_text)
            return "dm"
        except ColloquiError as exc:
            log.warning("DM to user %s failed (%s); posting to #tasks", user.id, exc)
    await client.send_message(str(row.tasks_channel_id), channel_text(mention(user)))
    return "channel"


def schedule(coro) -> None:
    """Fire-and-forget with logging; never lets a notification failure surface."""

    async def runner():
        try:
            await coro
        except Exception:
            log.exception("Notification failed")

    spawn(runner())


# ---- task notifications ----


def _task_link() -> str:
    return f"{settings.app_url.rstrip('/')}/tasks"


def _due_text(task: Task) -> str:
    if not task.due_at:
        return ""
    return f" — due {task.due_at.strftime('%b %-d, %Y %H:%M UTC').replace(' 00:00 UTC', '')}"


async def notify_task_event(
    task_id: uuid.UUID,
    event: str,
    actor_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
) -> None:
    """assignee_id is the assignee AT EVENT TIME, captured by the caller — the
    task row is reloaded in a fresh session after the commit, and trusting its current assignee lets
    a create-then-assign race double-notify."""
    async with SessionLocal() as db:
        task = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
        if task is None:
            return
        row = await colloqui.get_integration(db, task.org_id)
        chat_enabled = colloqui.is_enabled(row)
        assignee = await active_user(db, assignee_id)
        what = f"**{task.name}**{_due_text(task)}"
        push_body = f"{task.name}{_due_text(task)}"
        push_data = {"task_id": str(task.id), "kind": "task_assigned"}

        if event == "created":
            # Team visibility, not a personal notice: the #tasks post always
            # happens — but a push-preferring assignee gets a plain name, not
            # an @mention, so chat doesn't ping them on top of the APNs push.
            if chat_enabled:
                who = assignee.display_name if wants_push(assignee) else mention(assignee)
                await colloqui.client_for(row).send_message(
                    str(row.tasks_channel_id), f"📋 New task for {who}: {what}\n{_task_link()}"
                )
            if wants_push(assignee) and assignee.id != actor_id:
                await _push(db, assignee, "New task assigned", push_body, push_data)
        elif event == "assigned":
            if assignee is None or assignee.id == actor_id:
                return
            await deliver(
                db,
                task.org_id,
                assignee,
                title="Task assigned to you",
                body=push_body,
                data=push_data,
                dm_text=f"📋 Task assigned to you: {what}\n{_task_link()}",
                channel_text=lambda who: f"📋 Task reassigned to {who}: {what}\n{_task_link()}",
                chat=row,
            )
        elif event == "completed":
            # Only worth a #tasks post when someone finishes a task that was on
            # SOMEONE ELSE's plate — completing your own (or an unassigned) task
            # is not news you need announced back to you.
            if assignee is None or assignee.id == actor_id:
                return
            if chat_enabled:
                content = f"✅ {mention(assignee)} — task done: **{task.name}**"
                await colloqui.client_for(row).send_message(str(row.tasks_channel_id), content)


async def _send_due_reminder(db, row: ColloquiIntegration | None, task: Task) -> bool:
    """Deliver one due reminder. Returns whether it went out anywhere (False =
    leave it pending for the next pass, e.g. a push-only user whose device
    hasn't registered yet and no chat to fall back to)."""
    what = f"**{task.name}**{_due_text(task)}"
    sent = await deliver(
        db,
        task.org_id,
        await active_user(db, task.assignee_id),
        title="Task reminder",
        body=f"{task.name}{_due_text(task)}",
        data={"task_id": str(task.id), "kind": "task_due"},
        dm_text=f"⏰ Reminder: {what}\n{_task_link()}",
        channel_text=lambda who: f"⏰ {who} — reminder: {what}\n{_task_link()}",
        chat=row,
    )
    return sent is not None


async def _reminder_channel_exists(db, row: ColloquiIntegration | None, task: Task) -> bool:
    """Whether ANY channel could carry this task's reminder right now: the
    org's chat integration, or (for a push install) a registered device of the
    assignee. False means the reminder has nowhere to EVER land, as opposed to
    a channel that exists but transiently failed."""
    if colloqui.is_enabled(row):
        return True
    if not push.is_configured() or task.assignee_id is None:
        return False
    # Active assignees only — push.send_to_user refuses a deactivated account,
    # so a leftover device row is not a channel (it would retry every pass).
    return (
        await db.execute(
            select(DeviceToken.id)
            .join(User, DeviceToken.user_id == User.id)
            .where(DeviceToken.user_id == task.assignee_id, User.is_active.is_(True))
            .limit(1)
        )
    ).first() is not None


async def run_due_pass() -> None:
    """One sweep: notify every open task whose reminder time has passed and
    that hasn't been notified yet. An explicit reminder_at fires at that
    moment; a task with only a due time gets a default reminder
    task_reminder_lead_minutes BEFORE it's due (a reminder at the due moment
    is too late). Runs even when the chat integration is absent, so push-only
    installs still get reminders."""
    async with SessionLocal() as db:
        integrations = {
            r.org_id: r
            for r in (await db.execute(select(ColloquiIntegration))).scalars().all()
        }
        now = utcnow()
        lead = timedelta(minutes=settings.task_reminder_lead_minutes)
        due_tasks = (
            (
                await db.execute(
                    select(Task).where(
                        Task.status == "open",
                        Task.due_notified_at.is_(None),
                        or_(
                            Task.reminder_at <= now,
                            Task.reminder_at.is_(None) & (Task.due_at <= now + lead),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        for task in due_tasks:
            row = integrations.get(task.org_id)
            if not colloqui.is_enabled(row) and not push.is_configured():
                continue  # nowhere to deliver; same skip as before push existed
            try:
                if await _send_due_reminder(db, row, task):
                    task.due_notified_at = utcnow()
                    await db.commit()
                elif not await _reminder_channel_exists(db, row, task):
                    # Undeliverable, not transient — no chat and no device.
                    # Stamp it so it stops retrying every pass; editing the
                    # due/reminder time re-arms it once a channel appears.
                    log.warning(
                        "Reminder for task %s has no deliverable channel; marking notified",
                        task.id,
                    )
                    task.due_notified_at = utcnow()
                    await db.commit()
            except ColloquiError as exc:
                log.warning("Due reminder for task %s failed: %s", task.id, exc)
                await db.rollback()


async def reminder_loop() -> None:
    while True:
        try:
            await run_due_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Reminder pass failed")
        await asyncio.sleep(REMINDER_INTERVAL_SECONDS)
