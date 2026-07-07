import uuid

from fastapi import APIRouter
from sqlalchemy import select

from app.models import Company, Opportunity, Person
from app.schemas import OpportunityIn
from app.services.common import display_name_map
from app.services.crud import register_crud

router = APIRouter()


async def _name_map(db, model, ids: set, label) -> dict[str, str]:
    ids = {uuid.UUID(i) for i in ids if i}
    if not ids:
        return {}
    rows = await db.execute(select(model.id, label).where(model.id.in_(ids)))
    return {str(rid): name for rid, name in rows}


async def enrich(db, user, dicts):
    owners = await display_name_map(db, {d.get("owner_id") for d in dicts})
    companies = await _name_map(db, Company, {d.get("company_id") for d in dicts}, Company.name)
    person_ids = {uuid.UUID(d["primary_person_id"]) for d in dicts if d.get("primary_person_id")}
    people = {}
    if person_ids:
        rows = await db.execute(
            select(Person.id, Person.first_name, Person.last_name).where(Person.id.in_(person_ids))
        )
        people = {
            str(pid): " ".join(filter(None, [first, last])) for pid, first, last in rows
        }
    for d in dicts:
        d["owner_name"] = owners.get(d.get("owner_id"))
        d["company_name"] = companies.get(d.get("company_id"))
        d["person_name"] = people.get(d.get("primary_person_id"))


register_crud(
    router,
    model=Opportunity,
    entity_type="opportunity",
    body_model=OpportunityIn,
    search_cols=[Opportunity.name, Opportunity.source],
    sortable={
        "name": Opportunity.name,
        "status": Opportunity.status,
        "value": Opportunity.value,
        "close_date": Opportunity.close_date,
        "win_probability": Opportunity.win_probability,
        "created_at": Opportunity.created_at,
        "updated_at": Opportunity.updated_at,
    },
    filterable={
        "status": Opportunity.status,
        "pipeline_id": Opportunity.pipeline_id,
        "stage_id": Opportunity.stage_id,
        "company_id": Opportunity.company_id,
        "owner_id": Opportunity.owner_id,
        "primary_person_id": Opportunity.primary_person_id,
    },
    default_sort="created_at",
    required_any=["name"],
    enrich=enrich,
)
