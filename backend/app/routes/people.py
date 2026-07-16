import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import Company, Lead, Opportunity, Person, User
from app.schemas import PersonIn
from app.services.common import display_name_map
from app.services.crud import register_crud
from app.services.interactions import update_person_aggregates
from app.services.socials import find_for_person


def _hide_self_filter(request, user, stmt):
    """When hide_self=1, drop the contact record that is the logged-in user
    (matched by email). The record still exists and is editable directly —
    it's just kept out of the everyday People list."""
    if not request.query_params.get("hide_self"):
        return stmt
    own = (user.email or "").lower()
    if not own:
        return stmt
    return stmt.where(
        func.coalesce(func.lower(Person.work_email), "") != own,
        func.coalesce(func.lower(Person.personal_email), "") != own,
    )

router = APIRouter()


async def company_name_map(db, org_id, ids: set) -> dict[str, str]:
    ids = {uuid.UUID(i) for i in ids if i}
    if not ids:
        return {}
    rows = await db.execute(
        select(Company.id, Company.name).where(
            Company.id.in_(ids), Company.org_id == org_id
        )
    )
    return {str(cid): name for cid, name in rows}


async def enrich(db, user, dicts):
    owners = await display_name_map(db, {d.get("owner_id") for d in dicts}, user.org_id)
    companies = await company_name_map(db, user.org_id, {d.get("company_id") for d in dicts})
    for d in dicts:
        d["owner_name"] = owners.get(d.get("owner_id"))
        d["company_name"] = companies.get(d.get("company_id"))


register_crud(
    router,
    model=Person,
    entity_type="person",
    body_model=PersonIn,
    search_cols=[
        Person.first_name,
        Person.last_name,
        Person.work_email,
        Person.personal_email,
        Person.title,
    ],
    sortable={
        "last_name": Person.last_name,
        "first_name": Person.first_name,
        "contact_type": Person.contact_type,
        "last_contacted_at": Person.last_contacted_at,
        "interaction_count": Person.interaction_count,
        "created_at": Person.created_at,
        "updated_at": Person.updated_at,
    },
    filterable={
        "contact_type": Person.contact_type,
        "company_id": Person.company_id,
        "owner_id": Person.owner_id,
    },
    default_sort="last_name",
    required_any=["first_name", "last_name"],
    enrich=enrich,
    fk_checks={"company_id": Company, "owner_id": User},
    merge_refs=[(Opportunity, "primary_person_id"), (Lead, "converted_person_id")],
    after_merge=lambda db, user, target: update_person_aggregates(db, user.org_id, {target.id}),
    extra_filter=_hide_self_filter,
    merge_pool=[
        ["work_email", "personal_email"],
        ["work_phone", "mobile_phone"],
        ["work_website", "personal_website"],
    ],
)


@router.post("/{person_id}/find-socials")
async def find_socials(
    person_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mine the person's own emails and Gravatar for social profile links."""
    person = (
        await db.execute(
            select(Person).where(
                Person.id == person_id,
                Person.org_id == user.org_id,
                Person.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if person is None:
        raise HTTPException(status_code=404, detail="person not found")
    return await find_for_person(db, person)
