import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user, get_session_and_user, require_admin
from app.models import Session as DbSession
from app.models import User
from app.schemas import MeUpdateIn, ResetPasswordIn, UserAdminUpdateIn, UserCreateIn
from app.security import hash_password, verify_password

router = APIRouter()


def user_row(u: User) -> dict:
    return {
        "id": str(u.id),
        "email": u.email,
        "display_name": u.display_name,
        "is_admin": u.is_admin,
        "is_active": u.is_active,
        "totp_enabled": u.totp_enabled,
    }


@router.get("")
async def list_users(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    users = (
        (await db.execute(select(User).where(User.org_id == user.org_id).order_by(User.display_name)))
        .scalars()
        .all()
    )
    return {"items": [user_row(u) for u in users]}


@router.post("", status_code=201)
async def create_user(
    body: UserCreateIn, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
):
    exists = (
        await db.execute(
            select(User).where(User.org_id == admin.org_id, User.email == body.email.lower())
        )
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=409, detail="A user with that email already exists")
    if len(body.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    u = User(
        org_id=admin.org_id,
        email=body.email.lower(),
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        is_admin=body.is_admin,
    )
    db.add(u)
    await db.flush()
    result = user_row(u)
    await db.commit()  # visible before the admin's user list refetches
    return result


@router.patch("/me")
async def update_me(
    body: MeUpdateIn,
    session_user=Depends(get_session_and_user),
    db: AsyncSession = Depends(get_db),
):
    sess, user = session_user
    if body.display_name is not None:
        user.display_name = body.display_name.strip() or user.display_name
    if body.new_password:
        if not body.current_password or not verify_password(
            body.current_password, user.password_hash
        ):
            raise HTTPException(status_code=401, detail="Current password is incorrect")
        if len(body.new_password) < 8:
            raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
        user.password_hash = hash_password(body.new_password)
        # A password change invalidates every other session — a stolen token
        # must not survive the reset. This one stays.
        await db.execute(
            delete(DbSession).where(DbSession.user_id == user.id, DbSession.id != sess.id)
        )
    result = user_row(user)
    await db.commit()
    return result


@router.patch("/{user_id}")
async def update_user(
    user_id: uuid.UUID,
    body: UserAdminUpdateIn,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    u = (
        await db.execute(select(User).where(User.id == user_id, User.org_id == admin.org_id))
    ).scalar_one_or_none()
    if u is None:
        raise HTTPException(status_code=404, detail="User not found")
    if u.id == admin.id and body.is_admin is False:
        raise HTTPException(status_code=400, detail="You cannot remove your own admin role")
    if u.id == admin.id and body.is_active is False:
        raise HTTPException(status_code=400, detail="You cannot deactivate yourself")
    if body.is_admin is not None:
        u.is_admin = body.is_admin
    if body.is_active is not None:
        u.is_active = body.is_active
        if body.is_active is False:
            # Deactivation means out NOW, not when their session expires.
            await db.execute(delete(DbSession).where(DbSession.user_id == u.id))
    result = user_row(u)
    await db.commit()
    return result


@router.post("/{user_id}/reset-password")
async def reset_password(
    user_id: uuid.UUID,
    body: ResetPasswordIn,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin recovery for a user who forgot their password — there is no email
    infrastructure, so this is the only way back in."""
    u = (
        await db.execute(select(User).where(User.id == user_id, User.org_id == admin.org_id))
    ).scalar_one_or_none()
    if u is None:
        raise HTTPException(status_code=404, detail="User not found")
    if len(body.new_password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    u.password_hash = hash_password(body.new_password)
    await db.execute(delete(DbSession).where(DbSession.user_id == u.id))
    result = user_row(u)
    await db.commit()
    return result


@router.post("/{user_id}/reset-totp")
async def reset_totp(
    user_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin recovery for a user locked out of their authenticator."""
    u = (
        await db.execute(select(User).where(User.id == user_id, User.org_id == admin.org_id))
    ).scalar_one_or_none()
    if u is None:
        raise HTTPException(status_code=404, detail="User not found")
    u.totp_enabled = False
    u.totp_secret = None
    result = user_row(u)
    await db.commit()
    return result
