import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import (
    Company,
    CustomField,
    CustomFieldValue,
    Lead,
    Opportunity,
    Person,
    Pipeline,
    Stage,
    User,
    utcnow,
)
from app.schemas import ConvertIn, LeadIn
from app.services.common import (
    add_tags,
    custom_value_to_str,
    display_name_map,
    get_tag_maps,
    log_activity,
    overdue_task_entity_ids,
)
from app.services.crud import register_crud
from app.services.interactions import update_person_aggregates

router = APIRouter()

PERSON_FIELDS = [
    "first_name", "middle_name", "last_name", "prefix", "suffix", "title", "details",
    "street", "city", "state", "postal_code", "country", "work_phone", "mobile_phone",
    "work_website", "personal_website", "linkedin", "facebook", "owner_id",
]


async def enrich(db, user, dicts):
    owners = await display_name_map(db, {d.get("owner_id") for d in dicts}, user.org_id)
    overdue = await overdue_task_entity_ids(
        db, user.org_id, "lead", {d.get("id") for d in dicts}
    )
    for d in dicts:
        d["owner_name"] = owners.get(d.get("owner_id"))
        d["has_overdue_task"] = d.get("id") in overdue


register_crud(
    router,
    model=Lead,
    entity_type="lead",
    body_model=LeadIn,
    search_cols=[Lead.first_name, Lead.last_name, Lead.email, Lead.company_name, Lead.title],
    sortable={
        "last_name": Lead.last_name,
        "first_name": Lead.first_name,
        "status": Lead.status,
        "value": Lead.value,
        "created_at": Lead.created_at,
        "updated_at": Lead.updated_at,
    },
    filterable={"status": Lead.status, "owner_id": Lead.owner_id, "source": Lead.source},
    # Statuses are stored Title-Case ("New"); ?status=new must still match.
    ci_filters={"status"},
    default_sort="last_name",
    required_any=["first_name", "last_name"],
    enrich=enrich,
    fk_checks={"owner_id": User},
    merge_pool=[
        ["email"],
        ["work_phone", "mobile_phone"],
        ["work_website", "personal_website"],
    ],
)


async def _copy_custom_fields(
    db: AsyncSession, org_id: uuid.UUID, lead_id: uuid.UUID, person_id: uuid.UUID
) -> None:
    """Copy lead custom values to the new person, matching field definitions by
    name and creating person-side definitions when missing."""
    rows = (
        await db.execute(
            select(CustomField, CustomFieldValue.value)
            .join(CustomFieldValue, CustomFieldValue.field_id == CustomField.id)
            .where(
                CustomFieldValue.entity_type == "lead",
                CustomFieldValue.entity_id == lead_id,
            )
        )
    ).all()
    for lead_field, value in rows:
        person_field = (
            await db.execute(
                select(CustomField).where(
                    CustomField.org_id == org_id,
                    CustomField.entity_type == "person",
                    CustomField.name == lead_field.name,
                )
            )
        ).scalar_one_or_none()
        if person_field is None:
            person_field = CustomField(
                org_id=org_id,
                entity_type="person",
                name=lead_field.name,
                field_type=lead_field.field_type,
                options=lead_field.options,
                external_key=lead_field.external_key,
            )
            db.add(person_field)
            await db.flush()
        db.add(
            CustomFieldValue(
                field_id=person_field.id,
                entity_type="person",
                entity_id=person_id,
                # Same-named person field may be a different type than the
                # lead's; store the value in the person field's spelling.
                value=custom_value_to_str(person_field.field_type, value),
            )
        )


@router.post("/{lead_id}/convert")
async def convert_lead(
    lead_id: uuid.UUID,
    body: ConvertIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Claim the conversion first, in one conditional UPDATE. Two requests used
    # to both read converted_at IS NULL and both create a Person (and company,
    # and opportunity). The row lock this takes is held to the commit below,
    # so a concurrent convert waits, re-checks the WHERE, matches nothing and
    # gets the 409; if anything further down fails, the rollback un-claims.
    now = utcnow()
    claimed = (
        await db.execute(
            update(Lead)
            .where(
                Lead.id == lead_id,
                Lead.org_id == user.org_id,
                Lead.deleted_at.is_(None),
                Lead.converted_at.is_(None),
            )
            .values(status="Converted", converted_at=now, updated_at=now)
            .returning(Lead.id)
        )
    ).scalar_one_or_none()
    lead = (
        await db.execute(
            select(Lead).where(
                Lead.id == lead_id, Lead.org_id == user.org_id, Lead.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    if claimed is None:
        raise HTTPException(status_code=409, detail="Lead is already converted")

    company_id = None
    if body.company_id is not None:
        # The caller picked the company — the only unambiguous answer when
        # several share the lead's company name.
        picked = (
            await db.execute(
                select(Company.id).where(
                    Company.id == body.company_id,
                    Company.org_id == user.org_id,
                    Company.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if picked is None:
            raise HTTPException(status_code=404, detail="company not found")
        company_id = picked
    elif body.create_company and (lead.company_name or "").strip():
        name = lead.company_name.strip()
        # Company names aren't unique (the duplicates page exists for exactly
        # that), so a name can match several. Deterministic pick: the oldest
        # active match — the original, not whichever later copy.
        company = (
            await db.execute(
                select(Company)
                .where(
                    Company.org_id == user.org_id,
                    func.lower(Company.name) == name.lower(),
                    Company.deleted_at.is_(None),
                )
                .order_by(Company.created_at, Company.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if company is None:
            company = Company(org_id=user.org_id, name=name, owner_id=lead.owner_id)
            db.add(company)
            await db.flush()
            await log_activity(db, user.org_id, "company", company.id, "created", user.id)
        company_id = company.id

    person = Person(
        org_id=user.org_id,
        company_id=company_id,
        work_email=lead.email,
        **{f: getattr(lead, f) for f in PERSON_FIELDS},
    )
    db.add(person)
    await db.flush()
    # The lead's address may already have synced mail or calls behind it.
    await update_person_aggregates(db, user.org_id, {person.id})

    lead_tags = (await get_tag_maps(db, "lead", [lead.id])).get(lead.id, [])
    if lead_tags:
        await add_tags(db, user.org_id, "person", person.id, lead_tags)
    await _copy_custom_fields(db, user.org_id, lead.id, person.id)

    opportunity_id = None
    if body.pipeline_id is not None:
        # Verify the pipeline belongs to this org before deriving a stage from
        # it — never trust a client-supplied pipeline id to reach across orgs.
        pipeline = (
            await db.execute(
                select(Pipeline).where(
                    Pipeline.id == body.pipeline_id, Pipeline.org_id == user.org_id
                )
            )
        ).scalar_one_or_none()
        if pipeline is None:
            raise HTTPException(status_code=404, detail="pipeline not found")
        first_stage = (
            await db.execute(
                select(Stage)
                .where(Stage.pipeline_id == pipeline.id)
                .order_by(Stage.position)
                .limit(1)
            )
        ).scalar_one_or_none()
        opp = Opportunity(
            org_id=user.org_id,
            name=body.opportunity_name
            or " ".join(filter(None, [lead.first_name, lead.last_name])),
            company_id=company_id,
            primary_person_id=person.id,
            owner_id=lead.owner_id,
            value=body.opportunity_value if body.opportunity_value is not None else lead.value,
            currency=lead.currency,
            pipeline_id=body.pipeline_id,
            stage_id=first_stage.id if first_stage else None,
            win_probability=first_stage.win_probability if first_stage else None,
            source=lead.source,
        )
        db.add(opp)
        await db.flush()
        await log_activity(db, user.org_id, "opportunity", opp.id, "created", user.id)
        opportunity_id = opp.id

    # status/converted_at were set by the claim above.
    lead.converted_person_id = person.id

    await log_activity(
        db, user.org_id, "person", person.id, "created_from_lead", user.id,
        {"lead_id": str(lead.id)},
    )
    await log_activity(
        db, user.org_id, "lead", lead.id, "converted", user.id,
        {"person_id": str(person.id)},
    )

    result = {
        "person_id": str(person.id),
        "company_id": str(company_id) if company_id else None,
        "opportunity_id": str(opportunity_id) if opportunity_id else None,
    }
    # The client navigates to the new person immediately.
    await db.commit()
    return result
