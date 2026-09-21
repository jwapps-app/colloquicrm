"""Alembic URL handling, download headers, and tenant isolation."""

import asyncio
from urllib.parse import quote

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from .conftest import TEST_DATABASE_URL, make_company, make_person, run_alembic

ROLE = "crm_pct_role"
ROLE_PASSWORD = "p@ss"


async def _admin_execute(*statements: str) -> None:
    url = make_url(TEST_DATABASE_URL)
    conn = await asyncpg.connect(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        database=url.database,
    )
    try:
        for statement in statements:
            await conn.execute(statement)
    finally:
        await conn.close()


def _current_head() -> str:
    heads = run_alembic("heads")
    assert heads.returncode == 0, heads.stderr
    return heads.stdout.split()[0]


def test_alembic_accepts_a_percent_encoded_character_in_the_password():
    """Works against any server: the first character of the real password is
    sent percent-encoded, which is the same password with a % in the URL."""
    url = make_url(TEST_DATABASE_URL)
    password = url.password or ""
    encoded = f"%{ord(password[0]):02X}{quote(password[1:], safe='')}"
    raw = TEST_DATABASE_URL.replace(f":{quote(password, safe='')}@", f":{encoded}@", 1)
    assert "%" in raw
    result = run_alembic("current", database_url=raw)
    assert result.returncode == 0, result.stderr
    assert _current_head() in result.stdout


async def test_alembic_accepts_an_at_sign_in_the_password():
    """The audit's exact case: a password containing @, encoded as %40."""
    try:
        await _admin_execute(
            f"DROP ROLE IF EXISTS {ROLE}",
            f"CREATE ROLE {ROLE} LOGIN SUPERUSER PASSWORD '{ROLE_PASSWORD}'",
        )
    except asyncpg.InsufficientPrivilegeError:
        pytest.skip("test database user cannot create roles")
    try:
        url = make_url(TEST_DATABASE_URL)
        raw = (
            f"{url.drivername}://{ROLE}:p%40ss@{url.host}:{url.port or 5432}/{url.database}"
        )
        result = await asyncio.to_thread(run_alembic, "current", database_url=raw)
        assert result.returncode == 0, result.stderr
        assert _current_head() in result.stdout
    finally:
        await _admin_execute(f"DROP ROLE IF EXISTS {ROLE}")


async def _upload(client, auth, person, filename: str, content: bytes) -> dict:
    resp = await client.post(
        "/api/v1/attachments",
        headers=auth,
        data={"entity_type": "person", "entity_id": str(person.id)},
        files={"file": (filename, content, "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_unicode_filename_download_uses_rfc5987_and_attachment_disposition(
    client, world
):
    filename = "résumé – 日本語.pdf"
    person = await make_person(world.org)
    uploaded = await _upload(client, world.member_auth, person, filename, b"%PDF-1.4 test")
    assert uploaded["filename"] == filename

    resp = await client.get(
        f"/api/v1/attachments/{uploaded['id']}/download", headers=world.member_auth
    )
    assert resp.status_code == 200
    assert resp.content == b"%PDF-1.4 test"
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert f"filename*=utf-8''{quote(filename)}" in disposition
    disposition.encode("latin-1")  # a legal header: nothing raw leaked through
    assert resp.headers["x-content-type-options"] == "nosniff"


async def test_attachments_of_another_org_are_unreachable(client, world, other_world):
    person = await make_person(world.org)
    uploaded = await _upload(client, world.member_auth, person, "plan.pdf", b"secret plan")
    intruder = other_world.admin_auth

    listed = await client.get(
        "/api/v1/attachments",
        headers=intruder,
        params={"entity_type": "person", "entity_id": str(person.id)},
    )
    assert listed.json() == {"items": []}
    download = await client.get(
        f"/api/v1/attachments/{uploaded['id']}/download", headers=intruder
    )
    assert download.status_code == 404
    deleted = await client.delete(f"/api/v1/attachments/{uploaded['id']}", headers=intruder)
    assert deleted.status_code == 404
    onto_foreign = await client.post(
        "/api/v1/attachments",
        headers=intruder,
        data={"entity_type": "person", "entity_id": str(person.id)},
        files={"file": ("x.txt", b"x", "text/plain")},
    )
    assert onto_foreign.status_code == 404
    # Still there for its owner.
    mine = await client.get(
        f"/api/v1/attachments/{uploaded['id']}/download", headers=world.member_auth
    )
    assert mine.status_code == 200


async def test_records_of_another_org_are_unreachable_by_id(client, world, other_world):
    person = await make_person(world.org, work_email="pat@example.com")
    company = await make_company(world.org, "Globex")
    intruder = other_world.admin_auth
    for path in (f"/api/v1/people/{person.id}", f"/api/v1/companies/{company.id}"):
        assert (await client.get(path, headers=intruder)).status_code == 404, path
        patched = await client.patch(path, headers=intruder, json={"details": "pwned"})
        assert patched.status_code == 404, path
        assert (await client.delete(path, headers=intruder)).status_code == 404, path
    listing = await client.get("/api/v1/people", headers=intruder)
    assert listing.json()["items"] == []
    # A person in one org cannot be attached to a company in another.
    cross = await client.post(
        "/api/v1/people", headers=intruder, json={"first_name": "X", "company_id": str(company.id)}
    )
    assert cross.status_code == 422
    assert (await client.get(f"/api/v1/people/{person.id}", headers=world.member_auth)).json()[
        "details"
    ] is None


async def test_duplicate_detection_never_crosses_orgs(client, world, other_world):
    await make_person(world.org, first_name="Dana", last_name="Dupe", work_email="d@x.example")
    await make_person(
        other_world.org, first_name="Dana", last_name="Dupe", work_email="d@x.example"
    )
    for auth in (world.member_auth, other_world.member_auth):
        resp = await client.get(
            "/api/v1/duplicates", headers=auth, params={"entity_type": "person"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"groups": []}

    twin = await make_person(
        world.org, first_name="Dana", last_name="Dupe", work_email="D@x.example"
    )
    resp = await client.get(
        "/api/v1/duplicates", headers=world.member_auth, params={"entity_type": "person"}
    )
    (group,) = resp.json()["groups"]
    assert group["count"] == 2
    assert str(twin.id) in {item["id"] for item in group["items"]}
    other = await client.get(
        "/api/v1/duplicates", headers=other_world.member_auth, params={"entity_type": "person"}
    )
    assert other.json() == {"groups": []}
