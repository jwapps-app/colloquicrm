"""Lead-capture forms admin API. Any user can read the forms (to grab the
public URL); only admins create, edit, or delete them — a form is a public
door into the org's lead list. The public rendering/submission side lives in
routes/public_forms.py; this module owns the shared field catalog."""

import re
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import get_current_user, require_admin
from app.models import LeadForm, User, utcnow
from app.schemas import LeadFormIn, LeadFormUpdateIn

router = APIRouter()

# The v1 field catalog: key -> label, widget, and max length (mirrors the Lead
# column widths so a valid submission can never overflow the insert). Order
# here is the render order on the public page.
# KEEP IN SYNC with frontend/src/components/FormsSection.jsx FORM_FIELDS —
# the settings UI hand-mirrors this catalog for the embed snippet.
FORM_FIELD_DEFS: dict[str, dict] = {
    "first_name": {"label": "First name", "widget": "text", "max": 120},
    "last_name": {"label": "Last name", "widget": "text", "max": 120},
    "email": {"label": "Email", "widget": "email", "max": 255},
    "work_phone": {"label": "Phone", "widget": "tel", "max": 60},
    "company_name": {"label": "Company", "widget": "text", "max": 255},
    "title": {"label": "Title", "widget": "text", "max": 255},
    "details": {"label": "Message", "widget": "textarea", "max": 10000},
}
FIELD_ORDER = list(FORM_FIELD_DEFS)
ALWAYS_FIELDS = ("first_name", "last_name")
DEFAULT_SUCCESS = "Thanks — we'll be in touch shortly."

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def normalize_fields(fields: list, require_email: bool) -> list[str]:
    """Validate the admin's field picks and return them in canonical order.
    Name fields are always present; email is forced in when it's required."""
    if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
        raise HTTPException(status_code=422, detail="fields must be a list of field keys")
    unknown = sorted(set(fields) - set(FIELD_ORDER))
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown field keys: {', '.join(unknown)}. Allowed: {', '.join(FIELD_ORDER)}",
        )
    chosen = set(fields) | set(ALWAYS_FIELDS)
    if require_email:
        chosen.add("email")
    return [k for k in FIELD_ORDER if k in chosen]


def _slugify(name: str) -> str:
    base = _SLUG_RE.sub("-", name.lower()).strip("-")[:40].strip("-")
    return base or "form"


async def _unique_slug(db: AsyncSession, name: str) -> str:
    for _ in range(10):
        slug = f"{_slugify(name)}-{secrets.token_hex(3)}"
        taken = (
            await db.execute(select(LeadForm.id).where(LeadForm.slug == slug))
        ).scalar_one_or_none()
        if taken is None:
            return slug
    raise HTTPException(status_code=500, detail="Could not allocate a unique slug")


def public_url(form: LeadForm) -> str:
    return f"{settings.app_url.rstrip('/')}/f/{form.slug}"


def form_out(form: LeadForm) -> dict:
    return {
        "id": str(form.id),
        "name": form.name,
        "slug": form.slug,
        "enabled": form.enabled,
        "fields": form.fields or [],
        "require_email": form.require_email,
        "source": form.source,
        "success_message": form.success_message,
        "submission_count": form.submission_count,
        "public_url": public_url(form),
        "created_at": form.created_at.isoformat() if form.created_at else None,
        "updated_at": form.updated_at.isoformat() if form.updated_at else None,
    }


@router.get("")
async def list_forms(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    forms = (
        (
            await db.execute(
                select(LeadForm)
                .where(LeadForm.org_id == user.org_id)
                .order_by(LeadForm.created_at)
            )
        )
        .scalars()
        .all()
    )
    return {"items": [form_out(f) for f in forms]}


async def _get_form(db: AsyncSession, user: User, form_id: uuid.UUID) -> LeadForm:
    form = (
        await db.execute(
            select(LeadForm).where(LeadForm.id == form_id, LeadForm.org_id == user.org_id)
        )
    ).scalar_one_or_none()
    if form is None:
        raise HTTPException(status_code=404, detail="Form not found")
    return form


@router.post("", status_code=201)
async def create_form(
    body: LeadFormIn,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    form = LeadForm(
        org_id=user.org_id,
        name=name[:120],
        slug=await _unique_slug(db, name),
        enabled=body.enabled,
        fields=normalize_fields(body.fields, body.require_email),
        require_email=body.require_email,
        source=(body.source or "").strip()[:120] or name[:120],
        success_message=(body.success_message or "").strip() or DEFAULT_SUCCESS,
        created_by=user.id,
    )
    db.add(form)
    await db.flush()
    result = form_out(form)
    await db.commit()  # visible before the client refetches
    return result


@router.patch("/{form_id}")
async def update_form(
    form_id: uuid.UUID,
    body: LeadFormUpdateIn,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    form = await _get_form(db, user, form_id)
    data = body.model_dump(exclude_unset=True)
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="name is required")
        form.name = name[:120]
    if "require_email" in data:
        form.require_email = bool(data["require_email"])
    if "fields" in data or "require_email" in data:
        form.fields = normalize_fields(
            data.get("fields", form.fields or []), form.require_email
        )
    if "source" in data:
        form.source = (data["source"] or "").strip()[:120] or form.name
    if "success_message" in data:
        form.success_message = (data["success_message"] or "").strip() or DEFAULT_SUCCESS
    if "enabled" in data:
        form.enabled = bool(data["enabled"])
    form.updated_at = utcnow()
    result = form_out(form)
    await db.commit()  # visible before the client refetches
    return result


@router.delete("/{form_id}", status_code=204)
async def delete_form(
    form_id: uuid.UUID,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    form = await _get_form(db, user, form_id)
    await db.delete(form)
    await db.commit()
