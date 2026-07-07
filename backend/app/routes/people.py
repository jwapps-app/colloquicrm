import uuid

from fastapi import APIRouter
from sqlalchemy import select

from app.models import Company, Person
from app.schemas import PersonIn
from app.services.common import display_name_map
from app.services.crud import register_crud

router = APIRouter()


async def company_name_map(db, ids: set) -> dict[str, str]:
    ids = {uuid.UUID(i) for i in ids if i}
    if not ids:
        return {}
    rows = await db.execute(select(Company.id, Company.name).where(Company.id.in_(ids)))
    return {str(cid): name for cid, name in rows}


async def enrich(db, user, dicts):
    owners = await display_name_map(db, {d.get("owner_id") for d in dicts})
    companies = await company_name_map(db, {d.get("company_id") for d in dicts})
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
)


from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import User
from app.services.socials import find_for_person


@router.post("/{person_id}/find-socials")
async def find_socials(
    person_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mine the person's own emails and Gravatar for social profile links."""
    person = (
        await db.execute(select(Person).where(Person.id == person_id, Person.org_id == user.org_id))
    ).scalar_one_or_none()
    if person is None:
        raise HTTPException(status_code=404, detail="person not found")
    return await find_for_person(db, person)
