"""Duplicate detection: bounded GROUP BY ... HAVING queries surface groups of
records that share a normalized email, an exact (case-insensitive) name, or a
company email domain. Read-only — the merge endpoint on each entity does the
actual folding."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, func, select, tuple_, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import get_current_user
from app.models import Company, Lead, Person, User
from app.services.common import entity_label
from app.services.google import normalize_email

router = APIRouter()

MAX_GROUPS = 50
MAX_ITEMS = 10


def _email_canon(col, dialect: str):
    """SQL twin of services.google.normalize_email, so grouping happens in the
    database instead of Python over the whole table: lowercase, and for Gmail
    inboxes strip the +suffix and dots from the local part. split_part isn't
    available on the SQLite dev database — there duplicates match on the
    lowercased address only (Postgres, the real deployment, gets the full
    Gmail folding)."""
    lowered = func.lower(func.trim(col))
    if dialect != "postgresql":
        return lowered
    local = func.split_part(lowered, "@", 1)
    domain = func.split_part(lowered, "@", 2)
    return case(
        (
            domain.in_(("gmail.com", "googlemail.com")),
            func.replace(func.split_part(local, "+", 1), ".", "").concat("@gmail.com"),
        ),
        else_=lowered,
    )


def _present(col):
    return col.is_not(None) & (func.trim(col) != "")


async def _email_groups(db, org_id, model, email_cols, dialect):
    """[(normalized email, [ids])] for addresses shared by 2+ records."""
    parts = [
        select(model.id.label("rid"), _email_canon(col, dialect).label("em")).where(
            model.org_id == org_id, model.deleted_at.is_(None), _present(col)
        )
        for col in email_cols
    ]
    sub = union_all(*parts).subquery()
    dup_emails = [
        em
        for (em,) in await db.execute(
            select(sub.c.em)
            .group_by(sub.c.em)
            .having(func.count(func.distinct(sub.c.rid)) > 1)
            .order_by(sub.c.em)
            .limit(MAX_GROUPS)
        )
    ]
    if not dup_emails:
        return []
    members: dict[str, list] = {em: [] for em in dup_emails}
    for em, rid in await db.execute(
        select(sub.c.em, sub.c.rid).where(sub.c.em.in_(dup_emails)).distinct()
    ):
        members[em].append(rid)
    return [(em, ids) for em, ids in members.items() if len(ids) > 1]


async def _pair_groups(db, org_id, model, col_a, col_b):
    """[((a, b), [ids])] for case-insensitive (col_a, col_b) pairs (both
    non-empty) shared by 2+ records — names, or (name-only) company names."""
    key_a, key_b = func.lower(func.trim(col_a)), func.lower(func.trim(col_b))
    base = [model.org_id == org_id, model.deleted_at.is_(None), _present(col_a), _present(col_b)]
    pairs = [
        (a, b)
        for a, b in await db.execute(
            select(key_a, key_b)
            .where(*base)
            .group_by(key_a, key_b)
            .having(func.count(model.id) > 1)
            .order_by(key_a, key_b)
            .limit(MAX_GROUPS)
        )
    ]
    if not pairs:
        return []
    members: dict[tuple, list] = {p: [] for p in pairs}
    for a, b, rid in await db.execute(
        select(key_a, key_b, model.id).where(*base, tuple_(key_a, key_b).in_(pairs))
    ):
        members[(a, b)].append(rid)
    return [(pair, ids) for pair, ids in members.items() if len(ids) > 1]


async def _load_records(db, org_id, model, ids: set) -> dict:
    if not ids:
        return {}
    rows = await db.execute(select(model).where(model.org_id == org_id, model.id.in_(ids)))
    return {obj.id: obj for obj in rows.scalars()}


async def _person_sublabels(db, org_id, people: dict) -> dict[uuid.UUID, str | None]:
    """A person's identifying line: an email, else their company's name."""
    out = {}
    need_company = {}
    for pid, p in people.items():
        out[pid] = p.work_email or p.personal_email
        if out[pid] is None and p.company_id is not None:
            need_company[pid] = p.company_id
    if need_company:
        rows = await db.execute(
            select(Company.id, Company.name).where(
                Company.org_id == org_id, Company.id.in_(set(need_company.values()))
            )
        )
        names = dict(rows.all())
        for pid, cid in need_company.items():
            out[pid] = names.get(cid)
    return out


@router.get("")
async def find_duplicates(
    entity_type: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if entity_type not in ("person", "company", "lead"):
        raise HTTPException(status_code=422, detail=f"Unknown entity type: {entity_type}")
    org_id = user.org_id
    dialect = db.get_bind().dialect.name

    # (reason, [ids]) per match dimension, primary dimension first — when the
    # same id set shows up under both, only the first (email) reason is kept.
    raw: list[tuple[str, list]] = []
    if entity_type == "person":
        for em, ids in await _email_groups(
            db, org_id, Person, (Person.work_email, Person.personal_email), dialect
        ):
            raw.append((f"Same email: {normalize_email(em)}", ids))
        name_groups = await _pair_groups(db, org_id, Person, Person.first_name, Person.last_name)
        model = Person
    elif entity_type == "lead":
        for em, ids in await _email_groups(db, org_id, Lead, (Lead.email,), dialect):
            raw.append((f"Same email: {normalize_email(em)}", ids))
        name_groups = await _pair_groups(db, org_id, Lead, Lead.first_name, Lead.last_name)
        model = Lead
    else:
        # Companies: exact name, then shared email domain.
        key = func.lower(func.trim(Company.name))
        base = [Company.org_id == org_id, Company.deleted_at.is_(None), _present(Company.name)]
        names = [
            n
            for (n,) in await db.execute(
                select(key).where(*base).group_by(key)
                .having(func.count(Company.id) > 1).order_by(key).limit(MAX_GROUPS)
            )
        ]
        name_members: dict[str, list] = {n: [] for n in names}
        if names:
            for n, rid in await db.execute(
                select(key, Company.id).where(*base, key.in_(names))
            ):
                name_members[n].append(rid)
        name_groups = [((n,), ids) for n, ids in name_members.items() if len(ids) > 1]
        for dom, ids in await _email_groups(db, org_id, Company, (Company.email_domain,), "generic"):
            raw.append((f"Same domain: {dom}", ids))
        model = Company

    all_ids = {rid for _, ids in raw for rid in ids} | {
        rid for _, ids in name_groups for rid in ids
    }
    records = await _load_records(db, org_id, model, all_ids)
    sublabels: dict = {}
    if entity_type == "person":
        sublabels = await _person_sublabels(db, org_id, records)
    elif entity_type == "lead":
        sublabels = {rid: r.email for rid, r in records.items()}
    else:
        sublabels = {rid: r.email_domain for rid, r in records.items()}

    groups = []
    seen: set[frozenset] = set()
    email_first = raw if entity_type != "company" else []
    name_reasons = []
    for pair, ids in name_groups:
        # Reason shows the name as actually stored on the first record.
        first = records.get(sorted(ids, key=lambda i: records[i].created_at)[0])
        if entity_type == "company":
            display = first.name.strip() if first else pair[0]
        else:
            display = entity_label(first) if first else " ".join(pair)
        name_reasons.append((f"Same name: {display}", ids))
    # Companies group by name first; people and leads by email first.
    ordered = name_reasons + raw if entity_type == "company" else email_first + name_reasons
    for reason, ids in ordered:
        key = frozenset(ids)
        if key in seen:
            continue
        seen.add(key)
        items = sorted(
            (records[rid] for rid in ids if rid in records), key=lambda r: r.created_at
        )[:MAX_ITEMS]
        groups.append(
            {
                "reason": reason,
                "entity_type": entity_type,
                "items": [
                    {
                        "id": str(r.id),
                        "label": entity_label(r) or "(unnamed)",
                        "sublabel": sublabels.get(r.id),
                        "created_at": r.created_at.isoformat(),
                    }
                    for r in items
                ],
            }
        )
        if len(groups) >= MAX_GROUPS:
            break
    return {"groups": groups}
