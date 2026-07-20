import uuid

from fastapi import APIRouter
from sqlalchemy import select

from app.models import Company, Opportunity, Person, Pipeline, Stage, User
from app.schemas import OpportunityIn
from app.services.common import display_name_map
from app.services.crud import register_crud

router = APIRouter()


async def _name_map(db, model, org_id, ids: set, label) -> dict[str, str]:
    ids = {uuid.UUID(i) for i in ids if i}
    if not ids:
        return {}
    rows = await db.execute(
        select(model.id, label).where(model.id.in_(ids), model.org_id == org_id)
    )
    return {str(rid): name for rid, name in rows}


async def enrich(db, user, dicts):
    owners = await display_name_map(db, {d.get("owner_id") for d in dicts}, user.org_id)
    companies = await _name_map(
        db, Company, user.org_id, {d.get("company_id") for d in dicts}, Company.name
    )
    person_ids = {uuid.UUID(d["primary_person_id"]) for d in dicts if d.get("primary_person_id")}
    people = {}
    if person_ids:
        rows = await db.execute(
            select(Person.id, Person.first_name, Person.last_name).where(
                Person.id.in_(person_ids), Person.org_id == user.org_id
            )
        )
        people = {
            str(pid): " ".join(filter(None, [first, last])) for pid, first, last in rows
        }
    for d in dicts:
        d["owner_name"] = owners.get(d.get("owner_id"))
        d["company_name"] = companies.get(d.get("company_id"))
        d["person_name"] = people.get(d.get("primary_person_id"))


async def _refresh_stage_probability(db, opp, old_values, actor):
    # Moving stage refreshes the win_probability snapshot from the new stage —
    # otherwise the number shown (and every weighted forecast) is the OLD
    # stage's. An explicit win_probability in the same PATCH wins. Runs
    # pre-commit, so the refresh lands with the stage change.
    if "stage_id" not in old_values or old_values["stage_id"] == opp.stage_id:
        return
    if "win_probability" in old_values or opp.stage_id is None:
        return
    stage = (
        await db.execute(select(Stage).where(Stage.id == opp.stage_id))
    ).scalar_one_or_none()
    if stage is not None:
        opp.win_probability = stage.win_probability


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
    after_update=_refresh_stage_probability,
    fk_checks={
        "company_id": Company,
        "primary_person_id": Person,
        "pipeline_id": Pipeline,
        "stage_id": Stage,
        "owner_id": User,
    },
)
