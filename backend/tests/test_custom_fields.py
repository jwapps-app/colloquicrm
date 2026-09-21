"""Checkbox value spelling and CSV-export formula sanitizing."""

import csv
import io

from sqlalchemy import select

from app.models import CustomFieldValue


async def _create_field(client, auth, name: str, field_type: str = "text") -> str:
    resp = await client.post(
        "/api/v1/custom-fields",
        headers=auth,
        json={"entity_type": "person", "name": name, "field_type": field_type},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_checkbox_values_round_trip_in_canonical_spelling(client, world, db_session):
    field_id = await _create_field(client, world.admin_auth, "Newsletter", "checkbox")
    created = await client.post(
        "/api/v1/people",
        headers=world.member_auth,
        json={"first_name": "Cleo", "custom_fields": {field_id: False}},
    )
    assert created.status_code == 201, created.text
    person_id = created.json()["id"]
    assert created.json()["custom_fields"] == {field_id: "false"}

    fetched = await client.get(f"/api/v1/people/{person_id}", headers=world.member_auth)
    assert fetched.json()["custom_fields"] == {field_id: "false"}

    updated = await client.patch(
        f"/api/v1/people/{person_id}",
        headers=world.member_auth,
        json={"custom_fields": {field_id: True}},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["custom_fields"] == {field_id: "true"}
    async with db_session() as db:
        stored = (await db.execute(select(CustomFieldValue.value))).scalars().all()
    assert stored == ["true"]


async def test_csv_export_sanitizes_custom_field_headers_and_cells(client, world):
    field_id = await _create_field(client, world.admin_auth, "=1+1")
    created = await client.post(
        "/api/v1/people",
        headers=world.member_auth,
        json={
            "first_name": "=HYPERLINK(\"http://evil.invalid\")",
            "last_name": "Safe",
            "custom_fields": {field_id: "@SUM(A1:A9)"},
        },
    )
    assert created.status_code == 201, created.text

    resp = await client.get("/api/v1/people/export", headers=world.member_auth)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(resp.text.lstrip("﻿"))))
    header, record = rows[0], dict(zip(rows[0], rows[1]))
    assert "=1+1" not in header
    assert "'=1+1" in header
    assert record["'=1+1"] == "'@SUM(A1:A9)"
    assert record["first_name"] == "'=HYPERLINK(\"http://evil.invalid\")"
    assert record["last_name"] == "Safe"
    # Nothing in the file starts a cell with a formula trigger.
    assert not any(cell[:1] in ("=", "+", "-", "@") for row in rows for cell in row)
