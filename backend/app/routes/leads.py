import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
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
    Stage,
    User,
    utcnow,
)
from app.schemas import ConvertIn, LeadIn
from app.services.common import (
    add_tags,
    display_name_map,
    get_tag_maps,
    log_activity,
)
from app.services.crud import register_crud

router = APIRouter()

PERSON_FIELDS = [
    "first_name", "middle_name", "last_name", "prefix", "suffix", "title", "details",
    "street", "city", "state", "postal_code", "country", "work_phone", "mobile_phone",
    "work_website", "personal_website", "linkedin", "facebook", "owner_id",
]


async def enrich(db, user, dicts):
    owners = await display_name_map(db, {d.get("owner_id") for d in dicts})
    for d in dicts:
        d["owner_name"] = owners.get(d.get("owner_id"))


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
    default_sort="last_name",
    required_any=["first_name", "last_name"],
    enrich=enrich,
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
                value=value,
            )
        )


@router.post("/{lead_id}/convert")
async def convert_lead(
    lead_id: uuid.UUID,
    body: ConvertIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    lead = (
        await db.execute(select(Lead).where(Lead.id == lead_id, Lead.org_id == user.org_id))
    ).scalar_one_or_none()
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    if lead.converted_at is not None:
        raise HTTPException(status_code=409, detail="Lead is already converted")

    company_id = None
    if body.create_company and (lead.company_name or "").strip():
        name = lead.company_name.strip()
        company = (
            await db.execute(
                select(Company).where(Company.org_id == user.org_id, Company.name == name)
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

    lead_tags = (await get_tag_maps(db, "lead", [lead.id])).get(lead.id, [])
    if lead_tags:
        await add_tags(db, user.org_id, "person", person.id, lead_tags)
    await _copy_custom_fields(db, user.org_id, lead.id, person.id)

    opportunity_id = None
    if body.pipeline_id is not None:
        first_stage = (
            await db.execute(
                select(Stage)
                .where(Stage.pipeline_id == body.pipeline_id)
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

    lead.status = "Converted"
    lead.converted_at = utcnow()
    lead.converted_person_id = person.id

    await log_activity(
        db, user.org_id, "person", person.id, "created_from_lead", user.id,
        {"lead_id": str(lead.id)},
    )
    await log_activity(
        db, user.org_id, "lead", lead.id, "converted", user.id,
        {"person_id": str(person.id)},
    )

    return {
        "person_id": str(person.id),
        "company_id": str(company_id) if company_id else None,
        "opportunity_id": str(opportunity_id) if opportunity_id else None,
    }
