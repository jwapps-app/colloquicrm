"""Report arithmetic: the cases the audit reproduced."""

from datetime import timedelta

from app.models import utcnow

from .conftest import make_lead, make_opportunity, pipelines_of


async def test_pipeline_report_counts_every_open_deal_at_its_own_probability(client, world):
    sales, stages = (await pipelines_of(world.org))["Sales"]
    qualified = stages["Qualified"]  # 25% — the deal's own 90% must win
    await make_opportunity(
        world.org,
        "Explicit probability",
        value=100,
        win_probability=90,
        pipeline_id=sales.id,
        stage_id=qualified.id,
    )
    await make_opportunity(world.org, "No pipeline or stage", value=300, win_probability=100)
    await make_opportunity(world.org, "Closed", value=999, status="won", closed_at=utcnow())
    await make_opportunity(world.org, "Trashed", value=999, deleted_at=utcnow())

    resp = await client.get("/api/v1/reports/pipeline", headers=world.member_auth)
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["totals"]["open_count"] == 2
    assert report["totals"]["open_value"] == 400.0
    assert report["totals"]["weighted_forecast"] == 390.0
    assert report["currency"] == "USD"
    assert report["mixed_currencies"] is False

    by_name = {p["name"]: p for p in report["pipelines"]}
    stage_row = next(s for s in by_name["Sales"]["stages"] if s["stage_id"] == str(qualified.id))
    assert (stage_row["count"], stage_row["total_value"], stage_row["weighted_value"]) == (
        1,
        100.0,
        90.0,
    )
    unassigned = by_name["Unassigned"]
    assert unassigned["unassigned"] is True
    assert unassigned["totals"]["open_count"] == 1
    assert unassigned["totals"]["open_value"] == 300.0
    assert unassigned["totals"]["weighted_forecast"] == 300.0


async def test_mixed_currencies_are_reported_side_by_side_never_summed(client, world):
    sales, stages = (await pipelines_of(world.org))["Sales"]
    common = {"pipeline_id": sales.id, "stage_id": stages["Proposal"].id}  # 50%
    await make_opportunity(world.org, "USD one", value=100, currency="USD", **common)
    await make_opportunity(world.org, "USD two", value=200, currency="usd", **common)
    await make_opportunity(world.org, "EUR", value=1000, currency="EUR", **common)

    pipeline = (await client.get("/api/v1/reports/pipeline", headers=world.member_auth)).json()
    assert pipeline["currency"] == "USD"
    assert pipeline["mixed_currencies"] is True
    totals = pipeline["totals"]
    assert totals["open_count"] == 3
    assert totals["open_value"] == 300.0  # not 1300
    assert totals["weighted_forecast"] == 150.0
    assert totals["by_currency"] == {
        "USD": {"open_count": 2, "open_value": 300.0, "weighted_forecast": 150.0},
        "EUR": {"open_count": 1, "open_value": 1000.0, "weighted_forecast": 500.0},
    }

    now = utcnow()
    await make_opportunity(world.org, "Won USD", value=50, status="won", closed_at=now)
    await make_opportunity(world.org, "Won USD 2", value=70, status="won", closed_at=now)
    await make_opportunity(
        world.org, "Won EUR", value=5000, currency="EUR", status="won", closed_at=now
    )
    sales_report = (
        await client.get(
            "/api/v1/reports/sales", headers=world.member_auth, params={"range": "30d"}
        )
    ).json()
    assert sales_report["mixed_currencies"] is True
    assert sales_report["summary"]["won_count"] == 3
    assert sales_report["summary"]["won_value"] == 120.0  # not 5120
    assert sales_report["by_currency"]["EUR"] == {
        "won_count": 1,
        "won_value": 5000.0,
        "lost_count": 0,
        "lost_value": 0.0,
    }
    assert sum(b["won_value"] for b in sales_report["series"]) == 120.0


async def test_lead_conversion_rate_never_exceeds_one(client, world):
    now = utcnow()
    await make_lead(world.org, first_name="Fresh", created_at=now - timedelta(days=2))
    for name in ("Old one", "Old two"):
        await make_lead(
            world.org,
            first_name=name,
            created_at=now - timedelta(days=200),
            converted_at=now - timedelta(days=1),
            status="Converted",
        )

    resp = await client.get(
        "/api/v1/reports/leads", headers=world.member_auth, params={"range": "30d"}
    )
    assert resp.status_code == 200, resp.text
    summary = resp.json()["summary"]
    assert summary["new_leads"] == 1
    assert summary["converted_in_period"] == 2
    assert summary["cohort_converted"] == 0
    assert summary["conversion_rate"] == 0.0
    for row in resp.json()["by_source"]:
        assert row["conversion_rate"] is None or 0.0 <= row["conversion_rate"] <= 1.0


async def test_reports_only_see_the_callers_org(client, world, other_world):
    await make_opportunity(world.org, "Ours", value=100, win_probability=50)
    report = (
        await client.get("/api/v1/reports/pipeline", headers=other_world.admin_auth)
    ).json()
    assert report["totals"]["open_count"] == 0
    assert report["totals"]["open_value"] == 0.0
