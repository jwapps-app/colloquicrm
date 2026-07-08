from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Session as DbSession
from app.models import User, utcnow
from app.security import hash_token


def as_utc(dt: datetime) -> datetime:
    """SQLite returns naive datetimes even for timezone=True columns; values
    are always stored as UTC, so attach the zone when missing."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return auth[7:].strip()


async def get_session_and_user(
    request: Request, db: AsyncSession = Depends(get_db)
) -> tuple[DbSession, User]:
    token = bearer_token(request)
    row = (
        await db.execute(
            select(DbSession, User)
            .join(User, DbSession.user_id == User.id)
            .where(DbSession.token_hash == hash_token(token))
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid session")
    sess, user = row
    if sess.pending_totp:
        raise HTTPException(status_code=401, detail="Two-factor verification required")
    if as_utc(sess.expires_at) < utcnow():
        await db.delete(sess)
        # The raise rolls the transaction back — commit or the row lives forever.
        await db.commit()
        raise HTTPException(status_code=401, detail="Session expired")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Account disabled")
    if utcnow() - as_utc(sess.last_seen_at) > timedelta(hours=1):
        sess.last_seen_at = utcnow()
    return sess, user


async def get_current_user(
    session_user: tuple[DbSession, User] = Depends(get_session_and_user),
) -> User:
    return session_user[1]


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin required")
    return user
