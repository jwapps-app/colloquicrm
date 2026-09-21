"""Login, TOTP and password-change throttles hold under concurrency."""

import asyncio
from datetime import timedelta

import pyotp
from sqlalchemy import select

from app.models import Session as DbSession
from app.models import utcnow
from app.routes import auth
from app.security import new_session_token
from app.services.throttle import SlidingWindowLimiter

from .conftest import PASSWORD, make_org, make_user


async def test_concurrent_bad_logins_cannot_outrun_the_limit(client, monkeypatch):
    org = await make_org()
    await make_user(org, "victim@example.com")
    verified = 0

    async def slow_verifier(password, password_hash):
        nonlocal verified
        verified += 1
        await asyncio.sleep(0.05)  # every request is in flight at once
        return False

    monkeypatch.setattr(auth, "verify_password", slow_verifier)
    responses = await asyncio.gather(
        *(
            client.post(
                "/api/v1/auth/login",
                json={"email": "victim@example.com", "password": f"guess-{i}"},
            )
            for i in range(30)
        )
    )
    statuses = sorted(r.status_code for r in responses)
    assert verified <= auth._FAIL_LIMIT
    assert statuses.count(401) == auth._FAIL_LIMIT
    assert statuses.count(429) == 30 - auth._FAIL_LIMIT


async def test_correct_login_still_works_after_failures_and_costs_nothing(client):
    org = await make_org()
    await make_user(org, "user@example.com")
    for _ in range(auth._FAIL_LIMIT - 1):
        bad = await client.post(
            "/api/v1/auth/login", json={"email": "user@example.com", "password": "wrong-pass"}
        )
        assert bad.status_code == 401
    # One attempt of budget left: a success must not spend it.
    for _ in range(3):
        good = await client.post(
            "/api/v1/auth/login", json={"email": "user@example.com", "password": PASSWORD}
        )
        assert good.status_code == 200, good.text
        assert good.json()["user"]["email"] == "user@example.com"
    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {good.json()['token']}"}
    )
    assert me.status_code == 200


async def test_concurrent_totp_guesses_cannot_exceed_the_attempt_counter(
    client, db_session, monkeypatch
):
    org = await make_org()
    secret = pyotp.random_base32()
    user = await make_user(org, "totp@example.com", totp_secret=secret, totp_enabled=True)
    pending_token, pending_hash = new_session_token()
    async with db_session() as db:
        db.add(
            DbSession(
                user_id=user.id,
                token_hash=pending_hash,
                pending_totp=True,
                expires_at=utcnow() + timedelta(minutes=10),
            )
        )
        await db.commit()

    # Take the in-process limiter out of the picture: this is about the
    # database-side counter on the pending session.
    monkeypatch.setattr(auth, "_failures", SlidingWindowLimiter(900, 10_000))
    checked = 0
    real_verify = pyotp.TOTP.verify

    def counting_verify(self, otp, *args, **kwargs):
        nonlocal checked
        checked += 1
        return real_verify(self, otp, *args, **kwargs)

    monkeypatch.setattr(pyotp.TOTP, "verify", counting_verify)
    wrong = "000000" if pyotp.TOTP(secret).now() != "000000" else "111111"
    responses = await asyncio.gather(
        *(
            client.post(
                "/api/v1/auth/totp", json={"pending_token": pending_token, "code": wrong}
            )
            for _ in range(20)
        )
    )
    assert {r.status_code for r in responses} == {401}
    assert checked <= auth._TOTP_ATTEMPT_LIMIT

    # The pending session is burned: even the right code no longer promotes it.
    final = await client.post(
        "/api/v1/auth/totp",
        json={"pending_token": pending_token, "code": pyotp.TOTP(secret).now()},
    )
    assert final.status_code == 401
    async with db_session() as db:
        rows = (
            await db.execute(select(DbSession).where(DbSession.token_hash == pending_hash))
        ).all()
    assert rows == []


async def test_wrong_current_password_is_401_then_throttled(client, world, monkeypatch):
    from app.routes import users

    attempts = 0

    async def verifier(password, password_hash):
        nonlocal attempts
        attempts += 1
        return password == PASSWORD

    monkeypatch.setattr(users, "verify_password", verifier)
    payload = {"current_password": "not-my-password", "new_password": "a-brand-new-password"}
    for _ in range(auth._FAIL_LIMIT):
        resp = await client.patch("/api/v1/users/me", headers=world.member_auth, json=payload)
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Current password is incorrect"}
    # The session itself is untouched by the failures…
    assert (await client.get("/api/v1/auth/me", headers=world.member_auth)).status_code == 200

    # …but the oracle is closed, even for the right password.
    resp = await client.patch(
        "/api/v1/users/me",
        headers=world.member_auth,
        json={"current_password": PASSWORD, "new_password": "a-brand-new-password"},
    )
    assert resp.status_code == 429
    assert attempts == auth._FAIL_LIMIT
