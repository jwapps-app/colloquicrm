"""Duplicate detection: bounded GROUP BY ... HAVING queries surface groups of
records that share a normalized email, an exact (case-insensitive) name, or a
company email domain. Read-only — the merge endpoint on each entity does the
actual folding."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, case, func, select, union_all
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


async def _ranked_groups(db, members) -> list[tuple[tuple, int, list]]:
    """[(key, true member count, [up to MAX_ITEMS ids, oldest first])] for the
    first MAX_GROUPS keys shared by 2+ records.

    `members` is a subquery with one row per (key…, record): columns `rid`,
    `created_at`, and every other column is part of the key. Both bounds are
    applied IN the database — a window function numbers each group's members
    and only the first MAX_ITEMS of each come back, next to the group's real
    size. A 5,000-strong group (an import run twice) costs ten rows here, not
    five thousand ids and then five thousand records."""
    key_cols = [c for c in members.c if c.key not in ("rid", "created_at")]
    groups = (
        select(*key_cols, func.count().label("n"))
        .group_by(*key_cols)
        .having(func.count() > 1)
        .order_by(*key_cols)
        .limit(MAX_GROUPS)
        .subquery()
    )
    ranked = select(
        *key_cols,
        members.c.rid,
        func.row_number()
        .over(partition_by=key_cols, order_by=(members.c.created_at, members.c.rid))
        .label("rn"),
    ).subquery()
    ranked_keys = [ranked.c[c.key] for c in key_cols]
    rows = await db.execute(
        select(*ranked_keys, ranked.c.rid, groups.c.n)
        .join(groups, and_(*(ranked.c[c.key] == groups.c[c.key] for c in key_cols)))
        .where(ranked.c.rn <= MAX_ITEMS)
        .order_by(*ranked_keys, ranked.c.rn)
    )
    out: dict[tuple, tuple[int, list]] = {}
    for row in rows:
        *key, rid, n = row
        out.setdefault(tuple(key), (n, []))[1].append(rid)
    return [(key, n, ids) for key, (n, ids) in out.items()]


async def _email_groups(db, org_id, model, email_cols, dialect):
    """Groups of records sharing a normalized address."""
    parts = [
        select(
            model.id.label("rid"),
            model.created_at.label("created_at"),
            _email_canon(col, dialect).label("em"),
        ).where(model.org_id == org_id, model.deleted_at.is_(None), _present(col))
        for col in email_cols
    ]
    every = union_all(*parts).subquery()
    # A person whose work and personal address are the same appears twice in
    # the union — collapse to one row per (address, record) before counting.
    members = (
        select(every.c.em, every.c.rid, func.min(every.c.created_at).label("created_at"))
        .group_by(every.c.em, every.c.rid)
        .subquery()
    )
    return await _ranked_groups(db, members)


async def _key_groups(db, org_id, model, *cols):
    """Groups of records sharing the case-insensitive value(s) of `cols` (all
    non-empty) — first+last names, or a company name."""
    members = (
        select(
            *(func.lower(func.trim(col)).label(f"k{i}") for i, col in enumerate(cols)),
            model.id.label("rid"),
            model.created_at.label("created_at"),
        )
        .where(model.org_id == org_id, model.deleted_at.is_(None), *(_present(c) for c in cols))
        .subquery()
    )
    return await _ranked_groups(db, members)


async def _load_records(db, org_id, model, ids: set) -> dict:
    # Bounded by construction (MAX_GROUPS x MAX_ITEMS per dimension), and
    # batched anyway so the IN() list can never approach the driver's
    # bind-parameter ceiling.
    out = {}
    id_list = list(ids)
    for i in range(0, len(id_list), 500):
        rows = await db.execute(
            select(model).where(model.org_id == org_id, model.id.in_(id_list[i : i + 500]))
        )
        out.update({obj.id: obj for obj in rows.scalars()})
    return out


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

    # (reason, true count, [ids]) per match dimension, primary dimension
    # first — when the same group shows up under both, only the first
    # (email) reason is kept.
    raw: list[tuple[str, int, list]] = []
    if entity_type == "person":
        for (em,), n, ids in await _email_groups(
            db, org_id, Person, (Person.work_email, Person.personal_email), dialect
        ):
            raw.append((f"Same email: {normalize_email(em)}", n, ids))
        name_groups = await _key_groups(db, org_id, Person, Person.first_name, Person.last_name)
        model = Person
    elif entity_type == "lead":
        for (em,), n, ids in await _email_groups(db, org_id, Lead, (Lead.email,), dialect):
            raw.append((f"Same email: {normalize_email(em)}", n, ids))
        name_groups = await _key_groups(db, org_id, Lead, Lead.first_name, Lead.last_name)
        model = Lead
    else:
        # Companies: exact name, then shared email domain.
        name_groups = await _key_groups(db, org_id, Company, Company.name)
        for (dom,), n, ids in await _email_groups(
            db, org_id, Company, (Company.email_domain,), "generic"
        ):
            raw.append((f"Same domain: {dom}", n, ids))
        model = Company

    all_ids = {rid for _, _, ids in raw for rid in ids} | {
        rid for _, _, ids in name_groups for rid in ids
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
    seen: set[tuple] = set()
    name_reasons = []
    for pair, n, ids in name_groups:
        # Reason shows the name as actually stored on the oldest record (ids
        # arrive oldest first).
        first = records.get(ids[0])
        if entity_type == "company":
            display = first.name.strip() if first else pair[0]
        else:
            display = entity_label(first) if first else " ".join(pair)
        name_reasons.append((f"Same name: {display}", n, ids))
    # Companies group by name first; people and leads by email first.
    ordered = name_reasons + raw if entity_type == "company" else raw + name_reasons
    for reason, n, ids in ordered:
        # Same members under two reasons (same email AND same name) is one
        # group. With members capped, "same" = same size and same first ten.
        key = (n, frozenset(ids))
        if key in seen:
            continue
        seen.add(key)
        items = [records[rid] for rid in ids if rid in records]
        groups.append(
            {
                "reason": reason,
                "entity_type": entity_type,
                # How many records are really in the group; `items` shows at
                # most MAX_ITEMS of them (the oldest).
                "count": n,
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
