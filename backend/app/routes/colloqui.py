import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user, require_admin
from app.models import ColloquiIntegration, User, utcnow
from app.schemas import ColloquiConnectIn, ColloquiLinkIn
from app.services.colloqui import (
    SERVICE_USERNAME,
    SPACE_NAME,
    ColloquiClient,
    ColloquiError,
    bootstrap_workspace,
    ensure_workspace,
    get_integration,
    is_enabled,
    leave_space,
    validate_base_url,
)

router = APIRouter()


class ConnectIn(ColloquiConnectIn):
    # Blank = "keep the stored key": the settings form never gets the saved
    # key back, so re-saving a configured integration arrives with it empty.
    api_key: str = ""


class LinkIn(ColloquiLinkIn):
    # The CRM user being mapped; omitted = the admin themself.
    user_id: uuid.UUID | None = None


def _status(row: ColloquiIntegration | None, user: User) -> dict:
    return {
        "configured": bool(row and row.base_url and row.api_key),
        "connected": is_enabled(row),
        "base_url": row.base_url if row else None,
        "space_name": SPACE_NAME,
        "space_id": str(row.space_id) if row and row.space_id else None,
        "tasks_channel_id": str(row.tasks_channel_id) if row and row.tasks_channel_id else None,
        "connected_at": row.connected_at.isoformat() if row and row.connected_at else None,
        "last_error": row.last_error if row else None,
        "me": {
            "colloqui_user_id": str(user.colloqui_user_id) if user.colloqui_user_id else None,
            "colloqui_username": user.colloqui_username,
        },
    }


@router.get("/status")
async def status(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return _status(await get_integration(db, user.org_id), user)


@router.post("/connect")
async def connect(
    body: ConnectIn,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        base_url = validate_base_url(body.base_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    row = await get_integration(db, admin.org_id)
    api_key = body.api_key.strip()
    if not api_key:
        # Unchanged secret: reuse what's stored — but only against the server
        # it was issued for. Sending the saved key to a newly typed URL would
        # hand it to whoever runs that host.
        if row is None or not row.api_key:
            raise HTTPException(status_code=422, detail="An API key is required")
        if row.base_url != base_url:
            raise HTTPException(
                status_code=422,
                detail="Enter the API key for the new server — the saved key belongs to the old one",
            )
        api_key = row.api_key
    client = ColloquiClient(base_url, api_key)
    bootstrap_note = None
    try:
        await client.users()  # validates reachability + key
        if await client.is_admin_key():
            # Admin key pasted: set up everything, keep only the service key.
            stored_key, space_id, channel_id = await bootstrap_workspace(client)
            bootstrap_note = (
                f'Set up automatically: service user "{SERVICE_USERNAME}" with its own '
                f'API key, the "{SPACE_NAME}" space, and #tasks. The admin key you '
                "pasted was used once and NOT stored — you can revoke it in Colloqui."
            )
        else:
            stored_key = api_key
            space_id, channel_id = await ensure_workspace(client)
    except ColloquiError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if row is None:
        row = ColloquiIntegration(org_id=admin.org_id, base_url=base_url, api_key=stored_key)
        db.add(row)
    else:
        if row.base_url != base_url:
            # User ids from a different server are meaningless here — clear
            # every stale account link so nobody DMs into the void.
            await db.execute(
                update(User)
                .where(User.org_id == admin.org_id)
                .values(colloqui_user_id=None, colloqui_username=None)
            )
        row.base_url = base_url
        row.api_key = stored_key
    row.space_id = uuid.UUID(space_id)
    row.tasks_channel_id = uuid.UUID(channel_id)
    row.connected_at = utcnow()
    row.last_error = None
    await db.flush()

    # The space may be freshly provisioned (new server or re-created under a
    # new service user) — re-enroll everyone who already linked an account.
    linked = (
        (
            await db.execute(
                select(User).where(
                    User.org_id == admin.org_id, User.colloqui_user_id.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    service_client = ColloquiClient(base_url, stored_key)
    for u in linked:
        try:
            await service_client.add_space_member(space_id, str(u.colloqui_user_id))
        except ColloquiError:
            pass
    status = _status(row, admin)
    status["bootstrap_note"] = bootstrap_note
    await db.commit()  # visible before the client refetches
    return status


@router.delete("/connect", status_code=204)
async def disconnect(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    row = await get_integration(db, admin.org_id)
    if row is not None:
        await db.delete(row)
        await db.commit()  # visible before the client refetches


@router.post("/test")
async def send_test(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    row = await get_integration(db, admin.org_id)
    if not is_enabled(row):
        raise HTTPException(status_code=400, detail="Colloqui is not connected")
    try:
        await ColloquiClient(row.base_url, row.api_key).send_message(
            str(row.tasks_channel_id),
            "👋 Test message from the CRM — the integration is working.",
        )
    except ColloquiError as exc:
        row.last_error = str(exc)[:500]
        # The raise makes get_db roll back — commit or the recorded error
        # never lands in last_error.
        await db.commit()
        raise HTTPException(status_code=502, detail=str(exc))
    return {"sent": True}


@router.get("/users")
async def colloqui_users(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    row = await get_integration(db, user.org_id)
    if not row or not row.base_url or not row.api_key:
        raise HTTPException(status_code=400, detail="Colloqui is not configured")
    try:
        users = await ColloquiClient(row.base_url, row.api_key).users()
    except ColloquiError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return [
        {"id": u["id"], "username": u["username"], "display_name": u.get("display_name") or u["username"]}
        for u in users
    ]


async def _link_target(db: AsyncSession, admin: User, user_id: uuid.UUID | None) -> User:
    """The CRM user being mapped — the caller by default, otherwise a member
    of the same org (link additionally insists they're active)."""
    if user_id is None or user_id == admin.id:
        return admin
    target = (
        await db.execute(select(User).where(User.id == user_id, User.org_id == admin.org_id))
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    return target


@router.post("/link")
async def link_account(
    body: LinkIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Map a CRM user to a chat account. Admin-only: nothing here proves the
    caller controls the remote account, and linking both routes that person's
    task reminders to it and invites it into the CRM space — a member could
    otherwise point either at any account on the chat server."""
    if not user.is_admin:
        raise HTTPException(
            status_code=403, detail="Ask an administrator to link your Colloqui account"
        )
    target = await _link_target(db, user, body.user_id)
    if not target.is_active:
        raise HTTPException(status_code=422, detail="That user is deactivated")
    row = await get_integration(db, user.org_id)
    if not row or not row.base_url or not row.api_key:
        raise HTTPException(status_code=400, detail="Colloqui is not configured")
    client = ColloquiClient(row.base_url, row.api_key)
    # The id must exist on the currently connected server — a link to a ghost
    # user silently breaks DM reminders and space membership.
    try:
        remote = next(
            (u for u in await client.users() if u["id"] == str(body.colloqui_user_id)), None
        )
    except ColloquiError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if remote is None:
        raise HTTPException(status_code=404, detail="That user does not exist on the Colloqui server")
    target.colloqui_user_id = body.colloqui_user_id
    # Always the server's own record — a client-supplied username is ignored
    # (it ends up in @mentions, and must match the id it sits beside).
    target.colloqui_username = remote["username"]
    # Membership is what makes them see #tasks and receive its notifications.
    if is_enabled(row):
        try:
            await client.add_space_member(str(row.space_id), str(body.colloqui_user_id))
        except ColloquiError as exc:
            raise HTTPException(
                status_code=502, detail=f"Linked, but could not join the space: {exc}"
            )
    await db.commit()  # visible before the client refetches
    return {
        "colloqui_user_id": str(target.colloqui_user_id),
        "colloqui_username": target.colloqui_username,
    }


@router.delete("/link", status_code=204)
async def unlink_account(
    user_id: uuid.UUID | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Anyone may drop their OWN link (it only ever reduces what reaches
    them); unlinking somebody else (?user_id=) is an admin action."""
    if user_id is not None and user_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin required")
    target = await _link_target(db, user, user_id)
    leaving = target.colloqui_user_id
    target.colloqui_user_id = None
    target.colloqui_username = None
    await db.commit()  # visible before the client refetches
    await leave_space(db, user.org_id, leaving)
