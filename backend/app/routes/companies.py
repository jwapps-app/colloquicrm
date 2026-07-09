from fastapi import APIRouter

from app.models import Company, Opportunity, Person
from app.schemas import CompanyIn
from app.services.common import display_name_map
from app.services.crud import register_crud

router = APIRouter()


async def enrich(db, user, dicts):
    owners = await display_name_map(db, {d.get("owner_id") for d in dicts})
    for d in dicts:
        d["owner_name"] = owners.get(d.get("owner_id"))


register_crud(
    router,
    model=Company,
    entity_type="company",
    body_model=CompanyIn,
    search_cols=[Company.name, Company.email_domain, Company.city],
    sortable={
        "name": Company.name,
        "email_domain": Company.email_domain,
        "contact_type": Company.contact_type,
        "created_at": Company.created_at,
        "updated_at": Company.updated_at,
    },
    filterable={"contact_type": Company.contact_type, "owner_id": Company.owner_id},
    default_sort="name",
    required_any=["name"],
    enrich=enrich,
    merge_refs=[(Person, "company_id"), (Opportunity, "company_id")],
    merge_pool=[["work_phone"], ["work_website"]],
)
