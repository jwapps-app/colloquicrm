from datetime import timedelta

import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import as_utc, bearer_token, get_current_user, get_session_and_user
from app.models import Org, Session as DbSession, User, utcnow
from app.schemas import LoginIn, SetupIn, TotpCodeIn, TotpVerifyIn
from app.security import hash_password, hash_token, new_session_token, verify_password

router = APIRouter()


def user_out(user: User) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "totp_enabled": user.totp_enabled,
    }


async def _create_session(db: AsyncSession, user: User, pending: bool = False) -> str:
    token, token_hash = new_session_token()
    ttl = timedelta(minutes=10) if pending else timedelta(days=settings.session_ttl_days)
    db.add(
        DbSession(
            user_id=user.id,
            token_hash=token_hash,
            pending_totp=pending,
            expires_at=utcnow() + ttl,
        )
    )
    return token


# Auth endpoints commit explicitly before returning. The framework's commit
# (get_db teardown) runs only after the response is sent, and the client
# fires authenticated requests the instant it has a token — on a slow-commit
# database those requests would still see the old session state and 401.


@router.get("/bootstrap")
async def bootstrap(db: AsyncSession = Depends(get_db)):
    count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    return {"needs_setup": count == 0, "app_name": settings.app_name}


@router.post("/setup", status_code=201)
async def setup(body: SetupIn, db: AsyncSession = Depends(get_db)):
    count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    if count > 0:
        raise HTTPException(status_code=403, detail="Setup already completed")
    if len(body.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    org = (await db.execute(select(Org))).scalars().first()
    if org is None:
        org = Org(name="Default")
        db.add(org)
        await db.flush()
    user = User(
        org_id=org.id,
        email=body.email.lower(),
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        is_admin=True,
    )
    db.add(user)
    await db.flush()
    token = await _create_session(db, user)
    await db.commit()
    return {"token": token, "user": user_out(user)}


@router.post("/login")
async def login(body: LoginIn, db: AsyncSession = Depends(get_db)):
    user = (
        await db.execute(select(User).where(User.email == body.email.lower()))
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Account disabled")
    if user.totp_enabled:
        pending = await _create_session(db, user, pending=True)
        await db.commit()
        return {"totp_required": True, "pending_token": pending}
    token = await _create_session(db, user)
    await db.commit()
    return {"token": token, "user": user_out(user)}


@router.post("/totp")
async def totp_verify(body: TotpVerifyIn, db: AsyncSession = Depends(get_db)):
    row = (
        await db.execute(
            select(DbSession, User)
            .join(User, DbSession.user_id == User.id)
            .where(DbSession.token_hash == hash_token(body.pending_token))
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid session")
    sess, user = row
    if not sess.pending_totp or as_utc(sess.expires_at) < utcnow():
        raise HTTPException(status_code=401, detail="Verification window expired; log in again")
    if not user.totp_secret or not pyotp.TOTP(user.totp_secret).verify(
        body.code.strip(), valid_window=1
    ):
        raise HTTPException(status_code=401, detail="Invalid code")
    sess.pending_totp = False
    sess.expires_at = utcnow() + timedelta(days=settings.session_ttl_days)
    await db.commit()
    return {"token": body.pending_token, "user": user_out(user)}


@router.post("/logout", status_code=204)
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    token = bearer_token(request)
    await db.execute(delete(DbSession).where(DbSession.token_hash == hash_token(token)))
    await db.commit()


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return user_out(user)


@router.post("/totp/setup")
async def totp_setup(
    session_user=Depends(get_session_and_user), db: AsyncSession = Depends(get_db)
):
    _, user = session_user
    if user.totp_enabled:
        raise HTTPException(status_code=400, detail="Two-factor is already enabled")
    secret = pyotp.random_base32()
    user.totp_secret = secret
    uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=user.email, issuer_name=settings.app_name
    )
    await db.commit()
    return {"secret": secret, "otpauth_url": uri}


@router.post("/totp/enable")
async def totp_enable(
    body: TotpCodeIn,
    session_user=Depends(get_session_and_user),
    db: AsyncSession = Depends(get_db),
):
    _, user = session_user
    if not user.totp_secret:
        raise HTTPException(status_code=400, detail="Run setup first")
    if not pyotp.TOTP(user.totp_secret).verify(body.code.strip(), valid_window=1):
        raise HTTPException(status_code=401, detail="Invalid code")
    user.totp_enabled = True
    await db.commit()
    return {"totp_enabled": True}


@router.post("/totp/disable")
async def totp_disable(
    body: TotpCodeIn,
    session_user=Depends(get_session_and_user),
    db: AsyncSession = Depends(get_db),
):
    _, user = session_user
    if not user.totp_enabled or not user.totp_secret:
        raise HTTPException(status_code=400, detail="Two-factor is not enabled")
    if not pyotp.TOTP(user.totp_secret).verify(body.code.strip(), valid_window=1):
        raise HTTPException(status_code=401, detail="Invalid code")
    user.totp_enabled = False
    user.totp_secret = None
    await db.commit()
    return {"totp_enabled": False}
