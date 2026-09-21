"""Lead status filtering and conversion determinism / exclusivity."""

import asyncio
from datetime import timedelta

from sqlalchemy import func, select

from app.models import Lead, Opportunity, Person, utcnow

from .conftest import make_company, make_lead, pipelines_of


async def test_default_lead_matches_status_filter_in_any_case(client, world):
    created = await client.post(
        "/api/v1/leads", headers=world.member_auth, json={"first_name": "Nia", "last_name": "New"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "New"
    for spelling in ("New", "new", "NEW"):
        resp = await client.get(
            "/api/v1/leads", headers=world.member_auth, params={"status": spelling}
        )
        assert resp.status_code == 200
        assert [item["id"] for item in resp.json()["items"]] == [created.json()["id"]], spelling
    none = await client.get(
        "/api/v1/leads", headers=world.member_auth, params={"status": "converted"}
    )
    assert none.json()["items"] == []


async def test_conversion_with_duplicate_company_names_picks_the_oldest(client, world):
    now = utcnow()
    newer = await make_company(world.org, "Globex", created_at=now - timedelta(days=1))
    oldest = await make_company(world.org, "globex", created_at=now - timedelta(days=30))
    await make_company(
        world.org, "Globex", created_at=now - timedelta(days=90), deleted_at=now
    )  # trashed: never a candidate
    lead = await make_lead(world.org, company_name="Globex", email="lee@globex.example")

    resp = await client.post(
        f"/api/v1/leads/{lead.id}/convert",
        headers=world.member_auth,
        json={"create_company": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["company_id"] == str(oldest.id)
    assert resp.json()["company_id"] != str(newer.id)


async def test_conversion_honours_an_explicitly_picked_company(client, world, other_world):
    picked = await make_company(world.org, "Globex")
    await make_company(world.org, "Globex", created_at=utcnow() - timedelta(days=30))
    foreign = await make_company(other_world.org, "Globex")
    lead = await make_lead(world.org, company_name="Globex")

    denied = await client.post(
        f"/api/v1/leads/{lead.id}/convert",
        headers=world.member_auth,
        json={"company_id": str(foreign.id)},
    )
    assert denied.status_code == 404
    resp = await client.post(
        f"/api/v1/leads/{lead.id}/convert",
        headers=world.member_auth,
        json={"company_id": str(picked.id)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["company_id"] == str(picked.id)


async def test_concurrent_conversions_have_exactly_one_winner(client, world, db_session):
    lead = await make_lead(world.org, company_name="Initech", value=500)
    sales, _ = (await pipelines_of(world.org))["Sales"]
    responses = await asyncio.gather(
        *(
            client.post(
                f"/api/v1/leads/{lead.id}/convert",
                headers=world.member_auth,
                json={"pipeline_id": str(sales.id)},
            )
            for _ in range(6)
        )
    )
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200] + [409] * 5
    winner = next(r for r in responses if r.status_code == 200).json()
    async with db_session() as db:
        people = (await db.execute(select(func.count()).select_from(Person))).scalar_one()
        deals = (await db.execute(select(func.count()).select_from(Opportunity))).scalar_one()
        stored = await db.get(Lead, lead.id)
    assert (people, deals) == (1, 1)
    assert str(stored.converted_person_id) == winner["person_id"]
    assert stored.status == "Converted"


async def test_lead_of_another_org_cannot_be_read_or_converted(client, world, other_world):
    lead = await make_lead(world.org)
    assert (
        await client.get(f"/api/v1/leads/{lead.id}", headers=other_world.admin_auth)
    ).status_code == 404
    convert = await client.post(
        f"/api/v1/leads/{lead.id}/convert", headers=other_world.admin_auth, json={}
    )
    assert convert.status_code == 404
