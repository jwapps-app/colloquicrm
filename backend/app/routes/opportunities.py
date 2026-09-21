import uuid
from datetime import date, datetime, time, timezone

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.models import Company, Opportunity, Person, Pipeline, Stage, User, utcnow
from app.schemas import CLOSED_OPPORTUNITY_STATUSES, OpportunityIn
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


def closed_at_for(close_date: date | None) -> datetime:
    """When a deal that is closing right now should be dated. Normally this
    instant — but someone logging a deal they won last week sets close_date
    to last week, and the report should agree with them. A close_date in the
    future is a forecast, not a fact, so it doesn't count."""
    now = utcnow()
    if close_date is not None and close_date <= now.date():
        return datetime.combine(close_date, time.min, tzinfo=timezone.utc)
    return now


async def _first_stage_id(db, pipeline_id) -> uuid.UUID | None:
    return (
        await db.execute(
            select(Stage.id)
            .where(Stage.pipeline_id == pipeline_id)
            .order_by(Stage.position, Stage.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _validate_opportunity(db, user, data, current):
    """Create and PATCH both come through here, before anything is written.
    `data` is the body (exclude_unset) and may be added to: whatever lands in
    it is applied to the record and shows up in the activity log.

    1. The EFFECTIVE (pipeline, stage) pair must agree. fk_checks has already
       proven each id is in-org; this proves the stage is one of the
       pipeline's. A PATCH may carry either half — the other half is whatever
       the record already has.
    2. A new deal with a stage starts from that stage's win probability.
    3. closed_at follows the open <-> won/lost/abandoned transition."""
    def has(key: str) -> bool:
        return key in data

    pipeline_id = data["pipeline_id"] if has("pipeline_id") else getattr(current, "pipeline_id", None)
    stage_id = data["stage_id"] if has("stage_id") else getattr(current, "stage_id", None)

    stage = None
    if stage_id is not None and (has("stage_id") or has("pipeline_id")):
        stage = (
            await db.execute(select(Stage).where(Stage.id == stage_id))
        ).scalar_one_or_none()
    if has("stage_id") and stage is not None:
        if pipeline_id is None and not has("pipeline_id"):
            # A stage on a deal with no pipeline yet: the stage says which.
            data["pipeline_id"] = stage.pipeline_id
        elif stage.pipeline_id != pipeline_id:
            raise HTTPException(
                status_code=422, detail="stage_id does not belong to the opportunity's pipeline"
            )
    elif has("pipeline_id") and stage_id is not None:
        # Pipeline-only change. The stage the deal was in means nothing in
        # another pipeline: start it at the new pipeline's first stage (or
        # with no stage, when it has none / the pipeline was cleared).
        if stage is None or stage.pipeline_id != pipeline_id:
            data["stage_id"] = (
                await _first_stage_id(db, pipeline_id) if pipeline_id is not None else None
            )

    if current is None and data.get("stage_id") is not None and data.get("win_probability") is None:
        # The PATCH path refreshes the snapshot when the stage moves
        # (_refresh_stage_probability); a deal CREATED in a stage used to get
        # none at all, so forecasts weighted it at zero.
        if stage is None or stage.id != data["stage_id"]:
            stage = (
                await db.execute(select(Stage).where(Stage.id == data["stage_id"]))
            ).scalar_one_or_none()
        if stage is not None:
            data["win_probability"] = stage.win_probability

    was_closed = current is not None and current.status in CLOSED_OPPORTUNITY_STATUSES
    new_status = data["status"] if has("status") else getattr(current, "status", None) or "open"
    is_closed = new_status in CLOSED_OPPORTUNITY_STATUSES
    close_date = data["close_date"] if has("close_date") else getattr(current, "close_date", None)
    if is_closed and not was_closed:
        data["closed_at"] = closed_at_for(close_date)
    elif was_closed and not is_closed:
        data["closed_at"] = None  # reopened — it hasn't closed (yet) after all
    elif is_closed and has("close_date") and close_date != current.close_date:
        # Correcting the close date of a closed deal is a statement about
        # when it closed. Any OTHER edit leaves closed_at alone — that is the
        # point of having it.
        data["closed_at"] = closed_at_for(close_date)
    elif is_closed and current.closed_at is None:
        data["closed_at"] = closed_at_for(close_date)  # closed before closed_at existed


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
    body_validator=_validate_opportunity,
    fk_checks={
        "company_id": Company,
        "primary_person_id": Person,
        "pipeline_id": Pipeline,
        "stage_id": Stage,
        "owner_id": User,
    },
)
