"""Colloqui chat: the HTTP client, workspace provisioning, and the org's
connection row.

Built against Colloqui's INTEGRATION.md contract — a colq_ API key bound to a
service user; everything the CRM does in chat is that user acting through the
normal APIs. The CRM provisions (and reuses) a dedicated space with a #tasks
channel.

What gets said in chat, and to whom, is decided in services/notifications.py;
this module only knows how to talk to the server.
"""

import logging
import uuid
from ipaddress import ip_address
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from app.models import ColloquiIntegration

log = logging.getLogger("colloqui")

SPACE_NAME = "Colloqui CRM"
TASKS_CHANNEL_NAME = "tasks"
SERVICE_USERNAME = "crm"
SERVICE_DISPLAY_NAME = "CRM"
SERVICE_KEY_NAME = "crm-app"
REMINDER_INTERVAL_SECONDS = 60


class ColloquiError(Exception):
    pass


# Cloud instance-metadata endpoints — the classic SSRF prize. The link-local
# check below already covers the IP form; these are the hostname forms.
_METADATA_HOSTS = {"metadata.google.internal", "metadata", "instance-data"}


def validate_base_url(raw: str) -> str:
    """Normalise the admin-supplied chat server URL, or raise ValueError with
    a message fit for a 422. This is a self-hosted product and the chat
    server legitimately lives on a private LAN, so RFC1918 stays allowed;
    what's refused is anything that isn't plain http(s), embedded
    credentials, link-local (where cloud metadata services live) and the
    metadata hostnames."""
    url = raw.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("Base URL must start with http:// or https://")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("Base URL needs a host")
    if parts.username or parts.password:
        raise ValueError("Base URL must not embed credentials")
    if host in _METADATA_HOSTS:
        raise ValueError("That host is not allowed")
    try:
        addr = ip_address(host)
    except ValueError:
        return url
    if addr.is_link_local or addr.is_multicast or addr.is_unspecified:
        raise ValueError("That address is not allowed")
    return url


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    # One pool for every ColloquiClient instance — the reminder loop posts a
    # DM a minute and each fresh AsyncClient was a new TLS handshake. Nothing
    # per-server (base URL, key) lives on it; each request carries its own.
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=10.0, limits=httpx.Limits(max_connections=10, max_keepalive_connections=5)
        )
    return _client


async def aclose() -> None:
    """Drain the shared pool; called once from app shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class ColloquiClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def _request(self, method: str, path: str, json: dict | None = None) -> httpx.Response:
        try:
            resp = await _get_client().request(
                method,
                f"{self.base_url}/api/v1{path}",
                json=json,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except httpx.HTTPError as exc:
            raise ColloquiError(f"Cannot reach Colloqui at {self.base_url}: {exc}") from exc
        if resp.status_code >= 400:
            # The upstream body goes to the server log, not the API error —
            # it's the chat server's (or a proxy's) text and belongs nowhere
            # near a client. Proxies (e.g. Cloudflare) answer with HTML
            # pages; flag that much so the admin knows to look at the tunnel.
            snippet = resp.text.lstrip()[:500]
            log.warning(
                "Colloqui %s %s -> %s: %s", method, path, resp.status_code, snippet or "(empty)"
            )
            hint = " (HTML error page — proxy/tunnel hiccup?)" if snippet.startswith("<") else ""
            raise ColloquiError(
                f"Colloqui server returned {resp.status_code} for {method} {path}{hint}"
            )
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

    async def remove_space_member(self, space_id: str, user_id: str) -> None:
        """DELETE /spaces/{id}/members/{user_id} — needs the service user to
        be a manager of the space (bootstrap makes it one). The chat server
        also drops their membership of every channel in the space."""
        try:
            await self._request("DELETE", f"/spaces/{space_id}/members/{user_id}")
        except ColloquiError as exc:
            # Not a member (any more) is fine; anything else propagates.
            if "404" not in str(exc):
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


def client_for(row: ColloquiIntegration) -> ColloquiClient:
    return ColloquiClient(row.base_url, row.api_key)


async def leave_space(db, org_id: uuid.UUID, colloqui_user_id) -> None:
    """Best-effort: take an unlinked or deactivated user's chat account out of
    the CRM space so they stop seeing #tasks. Call it AFTER the CRM-side
    change is committed — chat being down must never undo or fail that, so a
    miss is only logged, for an admin to finish by hand in Colloqui."""
    if colloqui_user_id is None:
        return
    row = await get_integration(db, org_id)
    if not is_enabled(row) or not row.space_id:
        return
    try:
        await client_for(row).remove_space_member(str(row.space_id), str(colloqui_user_id))
    except ColloquiError as exc:
        log.warning(
            "Could not remove Colloqui user %s from the CRM space (%s) — "
            "remove them in Colloqui manually",
            colloqui_user_id,
            exc,
        )
