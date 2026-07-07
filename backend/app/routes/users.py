import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user, require_admin
from app.models import User
from app.schemas import MeUpdateIn, UserAdminUpdateIn, UserCreateIn
from app.security import hash_password, verify_password

router = APIRouter()


def user_row(u: User) -> dict:
    return {
        "id": str(u.id),
        "email": u.email,
        "display_name": u.display_name,
        "is_admin": u.is_admin,
        "is_active": u.is_active,
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
    return user_row(u)


@router.patch("/me")
async def update_me(
    body: MeUpdateIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
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
    return user_row(user)


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
    if body.is_admin is not None:
        u.is_admin = body.is_admin
    if body.is_active is not None:
        u.is_active = body.is_active
    return user_row(u)
