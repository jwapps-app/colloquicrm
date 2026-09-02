"""Device registration for iOS push. The companion app POSTs its APNs token
on login (and whenever iOS rotates it) and DELETEs it on logout, mirroring
the Colloqui messaging app's contract."""

from fastapi import APIRouter, Depends
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import DeviceToken, User, utcnow
from app.schemas import DeviceIn

router = APIRouter()

# Tokens rotate on reinstall and a wiped device never DELETEs its own, so an
# account would otherwise accumulate dead rows forever — each one an APNs
# send per notification, and an unbounded table for anyone scripting
# registrations. Nobody has twenty live iPhones.
_MAX_DEVICES_PER_USER = 20


@router.post("", status_code=201)
async def register_device(
    body: DeviceIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    existing = (
        await db.execute(select(DeviceToken).where(DeviceToken.token == body.token))
    ).scalar_one_or_none()
    if existing is not None and existing.user_id != user.id:
        # The token is on record under a different user. Don't silently
        # re-point another user's row (a stolen token would otherwise hijack
        # their push registration): drop the old row and register fresh for the
        # caller, so the attacker gains nothing beyond registering their own
        # device. (iOS reissues a token per install, so a genuine token rarely
        # crosses accounts anyway.)
        await db.delete(existing)
        await db.flush()
        existing = None
    if existing is not None:
        # Same device, same user — refresh its metadata.
        existing.org_id = user.org_id
        existing.platform = body.platform
        existing.environment = body.environment
        existing.last_seen_at = utcnow()
    else:
        # Make room: the least recently seen registrations go first.
        mine = (
            (
                await db.execute(
                    select(DeviceToken)
                    .where(DeviceToken.user_id == user.id)
                    .order_by(DeviceToken.last_seen_at.asc(), DeviceToken.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        for stale in mine[: max(0, len(mine) - (_MAX_DEVICES_PER_USER - 1))]:
            await db.delete(stale)
        db.add(
            DeviceToken(
                org_id=user.org_id,
                user_id=user.id,
                token=body.token,
                platform=body.platform,
                environment=body.environment,
            )
        )
    await db.commit()  # registration must be durable before the app trusts it
    return {"ok": True}


@router.delete("/{token}", status_code=204)
async def unregister_device(
    token: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        delete(DeviceToken).where(DeviceToken.token == token, DeviceToken.user_id == user.id)
    )
    await db.commit()
