"""Colloqui integration: workspace provisioning, task posts, due-task DMs.

Built against Colloqui's INTEGRATION.md contract — a colq_ API key bound to a
service user; everything the CRM does in chat is that user acting through the
normal APIs. The CRM provisions (and reuses) a dedicated space with a #tasks
channel: assignments post there for team visibility, and due reminders go as
a DM from the service user to the assignee's linked Colloqui account.

Every send is fire-and-forget: chat being down must never break or delay a
CRM action.
"""

import asyncio
import logging
import uuid

import httpx
from sqlalchemy import or_, select

from app.config import settings
from app.db import SessionLocal
from app.models import ColloquiIntegration, Task, User, utcnow

log = logging.getLogger("colloqui")

SPACE_NAME = "Colloqui CRM"
TASKS_CHANNEL_NAME = "tasks"
SERVICE_USERNAME = "crm"
SERVICE_DISPLAY_NAME = "CRM"
SERVICE_KEY_NAME = "crm-app"
REMINDER_INTERVAL_SECONDS = 60


class ColloquiError(Exception):
    pass


class ColloquiClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def _request(self, method: str, path: str, json: dict | None = None) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                base_url=f"{self.base_url}/api/v1",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=10.0,
            ) as client:
                resp = await client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise ColloquiError(f"Cannot reach Colloqui at {self.base_url}: {exc}") from exc
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise ColloquiError(f"Colloqui {method} {path} failed ({resp.status_code}): {detail}")
        return resp

    async def users(self) -> list[dict]:
        return (await self._request("GET", "/users")).json()

    async def spaces(self) -> list[dict]:
        return (await self._request("GET", "/spaces")).json()

    async def create_space(self, name: str) -> dict:
        return (await self._request("POST", "/spaces", {"name": name})).json()

    async def add_space_member(self, space_id: str, user_id: str, role: str = "member") -> None:
        try:
            await self._request(
                "POST", f"/spaces/{space_id}/members", {"user_id": user_id, "role": role}
            )
        except ColloquiError as exc:
            # Already a member is fine; anything else propagates.
            if "409" not in str(exc):
                raise

    async def is_admin_key(self) -> bool:
        """A key bound to an admin user can reach the admin API."""
        try:
            await self._request("GET", "/admin/api-keys")
            return True
        except ColloquiError:
            return False

    async def admin_create_user(self, username: str, display_name: str) -> None:
        try:
            await self._request(
                "POST", "/admin/users", {"username": username, "display_name": display_name}
            )
        except ColloquiError as exc:
            # Already exists is fine.
            if "409" not in str(exc):
                raise

    async def admin_create_api_key(self, name: str, username: str) -> str:
        resp = await self._request(
            "POST", "/admin/api-keys", {"name": name, "username": username}
        )
        return resp.json()["key"]

    async def channels(self) -> list[dict]:
        return (await self._request("GET", "/channels")).json()

    async def create_channel(self, space_id: str, name: str) -> dict:
        return (
            await self._request(
                "POST", "/channels", {"name": name, "is_private": False, "space_id": space_id}
            )
        ).json()

    async def open_dm(self, user_id: str) -> dict:
        return (await self._request("POST", "/dms", {"user_ids": [user_id]})).json()

    async def send_message(self, channel_id: str, content: str) -> dict:
        return (
            await self._request(
                "POST",
                f"/channels/{channel_id}/messages",
                {"content": content[:4000], "id": str(uuid.uuid4())},
            )
        ).json()


async def ensure_workspace(client: ColloquiClient) -> tuple[str, str]:
    """Find or create the CRM's space and its #tasks channel. Returns
    (space_id, tasks_channel_id)."""
    spaces = await client.spaces()
    space = next((s for s in spaces if s.get("name") == SPACE_NAME), None)
    if space is None:
        try:
            space = await client.create_space(SPACE_NAME)
        except ColloquiError as exc:
            if "403" in str(exc):
                raise ColloquiError(
                    "Colloqui only lets admins create spaces. In Colloqui, create a "
                    f'space named "{SPACE_NAME}" and add the service user to it as a '
                    "manager, then connect again."
                ) from exc
            raise
    space_id = space["id"]

    channels = await client.channels()
    channel = next(
        (
            c
            for c in channels
            if c.get("space_id") == space_id
            and c.get("name") == TASKS_CHANNEL_NAME
            and not c.get("is_dm")
        ),
        None,
    )
    if channel is None:
        try:
            channel = await client.create_channel(space_id, TASKS_CHANNEL_NAME)
        except ColloquiError as exc:
            if "409" not in str(exc):
                raise
            channels = await client.channels()
            channel = next(
                c
                for c in channels
                if c.get("space_id") == space_id and c.get("name") == TASKS_CHANNEL_NAME
            )
    return space_id, channel["id"]


async def bootstrap_workspace(admin_client: ColloquiClient) -> tuple[str, str, str]:
    """One-paste setup from an admin-bound key: create the service user, mint
    it a dedicated key, provision the space (service user as manager) and the
    #tasks channel. Returns (service_api_key, space_id, tasks_channel_id) —
    the admin key is used only for this call and never stored."""
    await admin_client.admin_create_user(SERVICE_USERNAME, SERVICE_DISPLAY_NAME)
    directory = await admin_client.users()
    service = next((u for u in directory if u["username"] == SERVICE_USERNAME), None)
    if service is None:
        raise ColloquiError(
            f'Created the "{SERVICE_USERNAME}" user but it does not appear in the user list'
        )
    service_key = await admin_client.admin_create_api_key(SERVICE_KEY_NAME, SERVICE_USERNAME)

    spaces = await admin_client.spaces()
    space = next((s for s in spaces if s.get("name") == SPACE_NAME), None)
    if space is None:
        space = await admin_client.create_space(SPACE_NAME)
    await admin_client.add_space_member(space["id"], service["id"], role="manager")

    service_client = ColloquiClient(admin_client.base_url, service_key)
    space_id, channel_id = await ensure_workspace(service_client)
    return service_key, space_id, channel_id


async def get_integration(db, org_id: uuid.UUID) -> ColloquiIntegration | None:
    return (
        await db.execute(
            select(ColloquiIntegration).where(ColloquiIntegration.org_id == org_id)
        )
    ).scalar_one_or_none()


def is_enabled(row: ColloquiIntegration | None) -> bool:
    return bool(row and row.base_url and row.api_key and row.tasks_channel_id)


def _client_for(row: ColloquiIntegration) -> ColloquiClient:
    return ColloquiClient(row.base_url, row.api_key)


def _task_link() -> str:
    return f"{settings.app_url.rstrip('/')}/tasks"


def _mention(assignee: User | None) -> str:
    if assignee is None:
        return "unassigned"
    if assignee.colloqui_username:
        return f"@{assignee.colloqui_username}"
    return assignee.display_name


def _due_text(task: Task) -> str:
    if not task.due_at:
        return ""
    return f" — due {task.due_at.strftime('%b %-d, %Y %H:%M UTC').replace(' 00:00 UTC', '')}"


def schedule(coro) -> None:
    """Fire-and-forget with logging; never lets a chat failure surface."""

    async def runner():
        try:
            await coro
        except Exception:
            log.exception("Colloqui notification failed")

    asyncio.create_task(runner())


async def notify_task_event(task_id: uuid.UUID, event: str) -> None:
    # Runs outside the request's transaction; give the commit a moment to land.
    await asyncio.sleep(1.0)
    async with SessionLocal() as db:
        task = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
        if task is None:
            return
        row = await get_integration(db, task.org_id)
        if not is_enabled(row):
            return
        assignee = None
        if task.assignee_id:
            assignee = (
                await db.execute(select(User).where(User.id == task.assignee_id))
            ).scalar_one_or_none()
        if event == "created":
            content = f"📋 New task for {_mention(assignee)}: **{task.name}**{_due_text(task)}\n{_task_link()}"
        elif event == "completed":
            content = f"✅ {_mention(assignee)} — task done: **{task.name}**"
        else:
            return
        await _client_for(row).send_message(str(row.tasks_channel_id), content)


async def _send_due_reminder(db, row: ColloquiIntegration, task: Task) -> None:
    assignee = None
    if task.assignee_id:
        assignee = (
            await db.execute(select(User).where(User.id == task.assignee_id))
        ).scalar_one_or_none()
    client = _client_for(row)
    content = f"⏰ Task due: **{task.name}**{_due_text(task)}\n{_task_link()}"
    if assignee is not None and assignee.colloqui_user_id:
        try:
            dm = await client.open_dm(str(assignee.colloqui_user_id))
            await client.send_message(dm["id"], content)
            return
        except ColloquiError as exc:
            # Stale/broken link must not strand the reminder — deliver it
            # where the team can see it instead of retrying forever.
            log.warning("DM reminder for task %s failed (%s); posting to #tasks", task.id, exc)
    await client.send_message(
        str(row.tasks_channel_id),
        f"⏰ {_mention(assignee)} — task due: **{task.name}**\n{_task_link()}",
    )


async def run_due_pass() -> None:
    """One sweep: DM (or post) a reminder for every open task whose
    reminder/due time has passed and that hasn't been notified yet."""
    async with SessionLocal() as db:
        integrations = (
            (await db.execute(select(ColloquiIntegration))).scalars().all()
        )
        now = utcnow()
        for row in integrations:
            if not is_enabled(row):
                continue
            due_tasks = (
                (
                    await db.execute(
                        select(Task).where(
                            Task.org_id == row.org_id,
                            Task.status == "open",
                            Task.due_notified_at.is_(None),
                            or_(
                                Task.reminder_at <= now,
                                Task.reminder_at.is_(None) & (Task.due_at <= now),
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for task in due_tasks:
                try:
                    await _send_due_reminder(db, row, task)
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
