"""Shared fixtures: a real PostgreSQL database migrated with Alembic, the app
driven in-process over ASGI, and factories for orgs, users and sessions.

The environment is configured BEFORE anything from `app` is imported —
settings, the engine and the attachment directory are all fixed at import.
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:test@localhost:5494/crm_test"
)
BACKEND_DIR = Path(__file__).resolve().parent.parent
_ATTACHMENTS_DIR = tempfile.mkdtemp(prefix="crm-test-attachments-")

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["ENVIRONMENT"] = "test"
os.environ["SECRET_KEY"] = "test-secret-key-not-used-anywhere-else-0123456789"
os.environ["SECRET_ENCRYPTION_KEY"] = ""
os.environ["ATTACHMENTS_DIR"] = _ATTACHMENTS_DIR
os.environ["TRUSTED_PROXY_IPS"] = "127.0.0.1/32"
# Push is "configured" so send_to_user takes the delivery path; the relay URL
# is unroutable and every outbound transport is blocked below anyway.
os.environ["PUSH_RELAY_URL"] = "http://push-relay.invalid"
os.environ["PUSH_RELAY_API_KEY"] = "test-relay-key"

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import DEFAULT_PIPELINES, app  # noqa: E402
from app.models import (  # noqa: E402
    Company,
    Lead,
    Opportunity,
    Org,
    Person,
    Pipeline,
    Stage,
    User,
    utcnow,
)
from app.models import Session as DbSession  # noqa: E402
from app.security import hash_password_sync, new_session_token  # noqa: E402

PASSWORD = "correct horse battery"
_PASSWORD_HASH = hash_password_sync(PASSWORD)


# --- database ---------------------------------------------------------------


async def _recreate_database() -> None:
    url = make_url(TEST_DATABASE_URL)
    # This DROPs the database. Never let a mistyped URL aim it at real data.
    if "test" not in (url.database or ""):
        raise RuntimeError(
            f"TEST_DATABASE_URL must name a test database (got {url.database!r}); "
            "it is dropped and recreated on every run"
        )
    conn = await asyncpg.connect(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        database="postgres",
    )
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{url.database}"')
    finally:
        await conn.close()


def run_alembic(*args: str, database_url: str = TEST_DATABASE_URL) -> subprocess.CompletedProcess:
    """Alembic in a subprocess, exactly as the container entrypoint runs it
    (env.py drives its own event loop, so it can't run inside ours)."""
    env = {**os.environ, "DATABASE_URL": database_url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture(scope="session", autouse=True)
def _migrated_database():
    asyncio.run(_recreate_database())
    result = run_alembic("upgrade", "head")
    assert result.returncode == 0, f"alembic upgrade head failed:\n{result.stderr}"
    yield


@pytest_asyncio.fixture(autouse=True)
async def _clean_state(_migrated_database):
    """Every test starts from empty tables and empty in-process limiters. The
    app commits explicitly, so a rolled-back outer transaction can't isolate
    tests — emptying the tables can. DELETE in dependency order rather than
    TRUNCATE: on tables this small it is an order of magnitude faster."""
    async with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    _reset_in_process_state()
    yield
    from app.services import background

    # Anything spawned in the background must finish inside its own test.
    pending = [t for t in getattr(background, "_tasks", ()) if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=10)


def _reset_in_process_state() -> None:
    from app.routes import attachments, auth, feed, public_forms
    from app.services import google

    for limiter in (auth._failures, attachments._uploads, public_forms._submissions):
        limiter._events.clear()
    attachments._reserved_bytes.clear()
    auth._verify_slots = None
    feed._maps_cache.clear()
    public_forms._form_day_counts.clear()
    google._syncing.clear()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _dispose_engine():
    yield
    await engine.dispose()
    shutil.rmtree(_ATTACHMENTS_DIR, ignore_errors=True)


# --- no real network --------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Push relay, Colloqui chat, Google, RingCentral and Gravatar are all
    reached through httpx. Any request that gets as far as a real transport
    fails the test; the in-process ASGI transport is a different class."""

    async def _blocked(self, request):
        raise AssertionError(f"real network call attempted: {request.method} {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked)


# --- HTTP client ------------------------------------------------------------


@pytest_asyncio.fixture
async def client():
    # No lifespan: the background loops (reminders, syncs, purge, automations)
    # never start under test.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture
def request_sessions():
    """The database sessions the app's requests run on, collected as they are
    opened — so a fake upstream can check none of them is sitting in a
    transaction while the "network" call is in flight. Mirrors get_db."""
    from app.db import get_db

    sessions: list = []

    async def tracked_get_db():
        async with SessionLocal() as session:
            sessions.append(session)
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = tracked_get_db
    yield sessions
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def db_session():
    """Session factory for arranging and inspecting rows directly."""
    return SessionLocal


# --- factories --------------------------------------------------------------


async def make_org(name: str = "Acme", pipelines: bool = True) -> Org:
    async with SessionLocal() as db:
        org = Org(name=name)
        db.add(org)
        await db.flush()
        if pipelines:
            for pos, (pname, stages) in enumerate(DEFAULT_PIPELINES):
                p = Pipeline(org_id=org.id, name=pname, position=pos)
                db.add(p)
                await db.flush()
                for spos, (sname, prob) in enumerate(stages):
                    db.add(
                        Stage(pipeline_id=p.id, name=sname, position=spos, win_probability=prob)
                    )
        await db.commit()
        return org


async def make_user(
    org: Org,
    email: str | None = None,
    *,
    admin: bool = False,
    active: bool = True,
    display_name: str | None = None,
    **fields,
) -> User:
    async with SessionLocal() as db:
        email = email or f"user-{uuid.uuid4().hex[:8]}@example.com"
        user = User(
            org_id=org.id,
            email=email,
            password_hash=_PASSWORD_HASH,
            display_name=display_name or email.split("@")[0],
            is_admin=admin,
            is_active=active,
            **fields,
        )
        db.add(user)
        await db.commit()
        return user


async def login(user: User) -> dict:
    """A live session for the user, minted directly (no argon2 round trip);
    returns the Authorization header."""
    token, token_hash = new_session_token()
    async with SessionLocal() as db:
        db.add(
            DbSession(
                user_id=user.id, token_hash=token_hash, expires_at=utcnow() + timedelta(days=1)
            )
        )
        await db.commit()
    return {"Authorization": f"Bearer {token}"}


async def add(*rows):
    """Persist ORM rows; returns them (detached, attributes loaded)."""
    async with SessionLocal() as db:
        db.add_all(rows)
        await db.commit()
    return rows[0] if len(rows) == 1 else rows


async def make_person(org: Org, **fields) -> Person:
    fields.setdefault("first_name", "Pat")
    fields.setdefault("last_name", "Person")
    return await add(Person(org_id=org.id, **fields))


async def make_company(org: Org, name: str = "Globex", **fields) -> Company:
    return await add(Company(org_id=org.id, name=name, **fields))


async def make_lead(org: Org, **fields) -> Lead:
    fields.setdefault("first_name", "Lee")
    fields.setdefault("last_name", "Lead")
    return await add(Lead(org_id=org.id, **fields))


async def make_opportunity(org: Org, name: str = "Deal", **fields) -> Opportunity:
    return await add(Opportunity(org_id=org.id, name=name, **fields))


async def pipelines_of(org: Org) -> dict[str, tuple[Pipeline, dict[str, Stage]]]:
    """{pipeline name: (pipeline, {stage name: stage})} for an org."""
    from sqlalchemy import select

    out = {}
    async with SessionLocal() as db:
        for p in (await db.execute(select(Pipeline).where(Pipeline.org_id == org.id))).scalars():
            stages = (
                await db.execute(select(Stage).where(Stage.pipeline_id == p.id))
            ).scalars()
            out[p.name] = (p, {s.name: s for s in stages})
    return out


class World:
    """One org with an admin and a regular member, both signed in."""

    org: Org
    admin: User
    member: User
    admin_auth: dict
    member_auth: dict


@pytest_asyncio.fixture
async def world() -> World:
    w = World()
    w.org = await make_org()
    w.admin = await make_user(w.org, "admin@example.com", admin=True, display_name="Ada Admin")
    w.member = await make_user(w.org, "member@example.com", display_name="Max Member")
    w.admin_auth = await login(w.admin)
    w.member_auth = await login(w.member)
    return w


@pytest_asyncio.fixture
async def other_world() -> World:
    """A second tenant, for cross-org isolation checks."""
    w = World()
    w.org = await make_org("Other Org")
    w.admin = await make_user(w.org, "admin@other.example", admin=True)
    w.member = await make_user(w.org, "member@other.example")
    w.admin_auth = await login(w.admin)
    w.member_auth = await login(w.member)
    return w
