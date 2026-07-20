import time
from collections import defaultdict
from datetime import timedelta
from ipaddress import ip_address

import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import as_utc, bearer_token, get_current_user, get_session_and_user
from app.models import Org, Session as DbSession, User, utcnow
from app.schemas import LoginIn, SetupIn, TotpCodeIn, TotpVerifyIn
from app.security import hash_password, hash_token, new_session_token, verify_password

router = APIRouter()

# A fixed valid argon2 hash to verify against when the supplied email doesn't
# exist, so a login for an unknown address does the same password-hashing work
# as one for a real user — no timing oracle for account enumeration. The
# plaintext is irrelevant; it just has to be a well-formed hash.
_DECOY_HASH = hash_password("timing-oracle-decoy")

# In-process throttle — single instance by design. Keyed by client IP and by
# account email so neither one address hammering many accounts nor many
# addresses hammering one account gets a free run.
_FAIL_WINDOW_SECONDS = 900
_FAIL_LIMIT = 10
_TOTP_ATTEMPT_LIMIT = 5
_failures: dict[str, list[float]] = defaultdict(list)


def _peer_is_trusted_proxy(peer: str) -> bool:
    try:
        addr = ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in settings.trusted_proxy_networks)


def _client_ip(request: Request) -> str:
    # cloudflared (or another reverse proxy) puts the real client in
    # cf-connecting-ip. Only believe it when the immediate peer is a proxy we
    # trust — otherwise a directly-reachable client could spoof the header to
    # dodge the per-IP throttle, so fall back to the peer address itself.
    peer = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("cf-connecting-ip")
    if forwarded and _peer_is_trusted_proxy(peer):
        return forwarded.strip()
    return peer


def _throttle(keys: list[str]) -> None:
    now = time.monotonic()
    for key in keys:
        recent = [t for t in _failures[key] if now - t < _FAIL_WINDOW_SECONDS]
        _failures[key] = recent
        if len(recent) >= _FAIL_LIMIT:
            raise HTTPException(
                status_code=429,
                detail="Too many failed attempts. Wait a few minutes and try again.",
            )


def _record_failure(keys: list[str]) -> None:
    now = time.monotonic()
    for key in keys:
        _failures[key].append(now)
    if len(_failures) > 10_000:  # bound memory under address-spraying
        _failures.clear()


def user_out(user: User) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "totp_enabled": user.totp_enabled,
        "notify_channel": user.notify_channel,
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
    if len(body.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    # Serialize concurrent /setup calls: a transaction-scoped Postgres advisory
    # lock means the second request blocks until the first commits, then sees
    # count > 0 below. (sqlite dev runs single-writer, so the count check alone
    # suffices there.) The IntegrityError catch is the final backstop — the
    # users (org_id, email) unique constraint rejects a duplicate first admin.
    if not settings.database_url.startswith("sqlite"):
        await db.execute(select(func.pg_advisory_xact_lock(0xC0110071)))
    count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    if count > 0:
        raise HTTPException(status_code=403, detail="Setup already completed")
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
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=403, detail="Setup already completed")
    token = await _create_session(db, user)
    await db.commit()
    return {"token": token, "user": user_out(user)}


@router.post("/login")
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    email = body.email.lower()
    throttle_keys = [f"ip:{_client_ip(request)}", f"email:{email}"]
    _throttle(throttle_keys)
    # Opportunistic hygiene: expired sessions otherwise accumulate forever.
    await db.execute(delete(DbSession).where(DbSession.expires_at < utcnow()))
    # Uniqueness is per (org_id, email) — the same address may exist in more
    # than one org, and scalar_one_or_none would 500 on it. This install is
    # single-org, so take the oldest match deterministically; true multi-org
    # login needs an org discriminator on the form.
    user = (
        (await db.execute(select(User).where(User.email == email).order_by(User.created_at)))
        .scalars()
        .first()
    )
    if user is None:
        # Do the same argon2 work an existing account would, so the response
        # time doesn't reveal whether the email is registered.
        verify_password(body.password, _DECOY_HASH)
        _record_failure(throttle_keys)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not verify_password(body.password, user.password_hash):
        _record_failure(throttle_keys)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Account disabled")
    if user.totp_enabled:
        # Minting a pending session is itself a throttled event: a stolen
        # password otherwise buys unlimited pending sessions (each good for a
        # fresh burst of TOTP guesses). Bound it by the same per-email + per-IP
        # budget as an outright wrong password.
        _record_failure(throttle_keys)
        pending = await _create_session(db, user, pending=True)
        await db.commit()
        return {"totp_required": True, "pending_token": pending}
    token = await _create_session(db, user)
    await db.commit()
    return {"token": token, "user": user_out(user)}


@router.post("/totp")
async def totp_verify(body: TotpVerifyIn, request: Request, db: AsyncSession = Depends(get_db)):
    # Keyed on IP AND the pending session itself, so neither a spoofable IP nor
    # a rotated pending token alone grants unlimited guesses. The per-user key
    # is added once the session resolves to a user (below).
    pending_hash = hash_token(body.pending_token)
    throttle_keys = [f"ip:{_client_ip(request)}", f"totp:{pending_hash}"]
    _throttle(throttle_keys)
    row = (
        await db.execute(
            select(DbSession, User)
            .join(User, DbSession.user_id == User.id)
            .where(DbSession.token_hash == pending_hash)
        )
    ).first()
    if row is None:
        _record_failure(throttle_keys)
        raise HTTPException(status_code=401, detail="Invalid session")
    sess, user = row
    # Bound guessing per account across however many pending sessions the
    # attacker mints, independent of the IP they arrive from.
    throttle_keys.append(f"totpuser:{user.id}")
    _throttle([f"totpuser:{user.id}"])
    if not sess.pending_totp or as_utc(sess.expires_at) < utcnow():
        raise HTTPException(status_code=401, detail="Verification window expired; log in again")
    if not user.totp_secret or not pyotp.TOTP(user.totp_secret).verify(
        body.code.strip(), valid_window=1
    ):
        # A pending session is not an oracle: a few wrong codes burn it.
        sess.totp_attempts += 1
        if sess.totp_attempts >= _TOTP_ATTEMPT_LIMIT:
            await db.delete(sess)
            await db.commit()
            _record_failure(throttle_keys)
            raise HTTPException(
                status_code=401, detail="Too many wrong codes; log in again"
            )
        await db.commit()
        _record_failure(throttle_keys)
        raise HTTPException(status_code=401, detail="Invalid code")
    sess.pending_totp = False
    sess.totp_attempts = 0
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


@router.get("/sessions")
async def list_sessions(
    session_user=Depends(get_session_and_user), db: AsyncSession = Depends(get_db)
):
    """The caller's active sessions for the Settings security view — where
    else you're signed in and when each session was last used (last_seen_at
    is refreshed at most hourly, so it's a coarse activity marker)."""
    sess, user = session_user
    rows = (
        (
            await db.execute(
                select(DbSession)
                .where(
                    DbSession.user_id == user.id,
                    DbSession.pending_totp.is_(False),
                    DbSession.expires_at >= utcnow(),
                )
                .order_by(DbSession.last_seen_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(s.id),
                "current": s.id == sess.id,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "last_seen_at": s.last_seen_at.isoformat() if s.last_seen_at else None,
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
            }
            for s in rows
        ]
    }


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
