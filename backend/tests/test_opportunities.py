"""Pipeline/stage integrity and the closed_at lifecycle of a deal."""

from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import Opportunity, utcnow

from .conftest import make_opportunity, pipelines_of


async def _create(client, auth, **body) -> dict:
    resp = await client.post("/api/v1/opportunities", headers=auth, json={"name": "Deal", **body})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_stage_from_another_pipeline_is_422_on_create_and_patch(client, world, db_session):
    pipes = await pipelines_of(world.org)
    sales, sales_stages = pipes["Sales"]
    _, bizdev_stages = pipes["Business Development"]
    foreign_stage = bizdev_stages["Discussion"]

    resp = await client.post(
        "/api/v1/opportunities",
        headers=world.member_auth,
        json={"name": "Mismatch", "pipeline_id": str(sales.id), "stage_id": str(foreign_stage.id)},
    )
    assert resp.status_code == 422
    async with db_session() as db:
        assert (await db.execute(select(Opportunity))).all() == []

    deal = await _create(
        client,
        world.member_auth,
        pipeline_id=str(sales.id),
        stage_id=str(sales_stages["Qualified"].id),
    )
    assert deal["win_probability"] == 25  # snapshot of the stage it was created in
    patched = await client.patch(
        f"/api/v1/opportunities/{deal['id']}",
        headers=world.member_auth,
        json={"stage_id": str(foreign_stage.id)},
    )
    assert patched.status_code == 422


async def test_pipeline_only_patch_resets_the_stage(client, world):
    pipes = await pipelines_of(world.org)
    sales, sales_stages = pipes["Sales"]
    bizdev, bizdev_stages = pipes["Business Development"]
    deal = await _create(
        client,
        world.member_auth,
        pipeline_id=str(sales.id),
        stage_id=str(sales_stages["Proposal"].id),
    )
    resp = await client.patch(
        f"/api/v1/opportunities/{deal['id']}",
        headers=world.member_auth,
        json={"pipeline_id": str(bizdev.id)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pipeline_id"] == str(bizdev.id)
    assert resp.json()["stage_id"] == str(bizdev_stages["Prospecting"].id)


async def test_closed_at_is_stamped_on_close_and_cleared_on_reopen(client, world):
    deal = await _create(client, world.member_auth, value=100)
    assert deal["closed_at"] is None

    before = utcnow()
    won = await client.patch(
        f"/api/v1/opportunities/{deal['id']}", headers=world.member_auth, json={"status": "won"}
    )
    assert won.status_code == 200, won.text
    stamped = won.json()["closed_at"]
    assert stamped is not None
    assert before - timedelta(seconds=5) <= datetime.fromisoformat(stamped) <= utcnow()

    # An unrelated edit leaves the stamp alone.
    edited = await client.patch(
        f"/api/v1/opportunities/{deal['id']}", headers=world.member_auth, json={"name": "Renamed"}
    )
    assert edited.json()["closed_at"] == stamped

    reopened = await client.patch(
        f"/api/v1/opportunities/{deal['id']}", headers=world.member_auth, json={"status": "open"}
    )
    assert reopened.json()["closed_at"] is None


async def test_editing_a_closed_deal_does_not_move_its_sales_report_period(client, world):
    long_ago = utcnow() - timedelta(days=100)
    deal = await make_opportunity(
        world.org,
        "Old win",
        status="won",
        value=1000,
        closed_at=long_ago,
        created_at=long_ago - timedelta(days=10),
        updated_at=long_ago,
    )

    async def won_count(range_: str) -> int:
        resp = await client.get(
            "/api/v1/reports/sales", headers=world.member_auth, params={"range": range_}
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["summary"]["won_count"]

    assert (await won_count("30d"), await won_count("12m")) == (0, 1)
    edited = await client.patch(
        f"/api/v1/opportunities/{deal.id}",
        headers=world.member_auth,
        json={"details": "Tidied up the notes"},
    )
    assert edited.status_code == 200, edited.text
    assert (await won_count("30d"), await won_count("12m")) == (0, 1)


async def test_opportunity_of_another_org_is_unreachable(client, world, other_world):
    deal = await make_opportunity(world.org, "Secret deal")
    sales, _ = (await pipelines_of(world.org))["Sales"]
    assert (
        await client.get(f"/api/v1/opportunities/{deal.id}", headers=other_world.admin_auth)
    ).status_code == 404
    patched = await client.patch(
        f"/api/v1/opportunities/{deal.id}", headers=other_world.admin_auth, json={"name": "x"}
    )
    assert patched.status_code == 404
    # Nor can another org's pipeline be referenced from a new deal.
    created = await client.post(
        "/api/v1/opportunities",
        headers=other_world.admin_auth,
        json={"name": "Borrowed", "pipeline_id": str(sales.id)},
    )
    assert created.status_code == 422
