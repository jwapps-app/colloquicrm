"""Copper-format CSV import: parse, detect duplicates, commit.

Preview is stateless — it parses the file and flags likely duplicates against
the database and within the file. The client sends the (possibly edited) rows
back to commit with a per-row action: create, skip, or merge.
"""

import csv
import io
import logging
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Company,
    CustomField,
    CustomFieldValue,
    EntityTag,
    ImportJob,
    Lead,
    Opportunity,
    Person,
    Pipeline,
    Stage,
    User,
    utcnow,
)
from app.schemas import CLOSED_OPPORTUNITY_STATUSES, canonical_lead_status
from app.services.common import (
    add_tags,
    canonical_checkbox,
    get_or_create_tag,
    log_activity,
)

log = logging.getLogger("importer")

COMMIT_CHUNK = 250

CF_HEADER = re.compile(r"^(.+?)\s+(cf_\d+)$")
# Relationship custom fields in the full export: "Referred cf_123 people names"
# (import the names as text) / "... people ids" (Copper-internal, skip).
CF_PEOPLE_HEADER = re.compile(r"^(.+?)\s+cf_\d+\s+people\s+(names|ids)$")

# Copper's full data export (Settings -> Export Data) uses value/type column
# pairs instead of the per-list export's named columns.
PAIR_HEADER = re.compile(r"^(Email|Phone Number|Social|Website)( \d+)?$")
PAIR_TYPE_HEADER = re.compile(r"^(Email|Phone Number|Social|Website)( \d+)? Type$")
PAIR_BUCKETS = {"Email": "emails", "Phone Number": "phones", "Social": "socials",
                "Website": "websites"}

# Full-export columns that mean nothing here — skipped without cluttering the
# "unmapped headers" warning.
SILENT_HEADERS = {
    "Copper ID", "Copper URL", "Owner Id", "Company Id", "Primary Contact ID",
    "Primary Contact", "Inactive Days", "Interaction Count", "Updated At",
    "Last Status At", "Converted Opportunity Id", "Converted Contact Id",
    "Completed Date", "Days in Stage", "Last Stage At", "Lead Created At",
    "Last Contacted",  # people map it; elsewhere it's computed data
}

IMPORT_TYPES = {
    "people": "person",
    "leads": "lead",
    "companies": "company",
    "opportunities": "opportunity",
}

_NAME_COLS = {
    "First Name": "first_name",
    "Middle Name": "middle_name",
    "Last Name": "last_name",
    "Prefix": "prefix",
    "Suffix": "suffix",
}
_ADDRESS_COLS = {
    "Street": "street",
    "City": "city",
    "State": "state",
    "Postal Code": "postal_code",
    "Country": "country",
}

HEADER_MAPS: dict[str, dict[str, str]] = {
    "people": {
        **_NAME_COLS,
        "Title": "title",
        "Details": "details",
        "Company": "company_name",
        "Owned By": "owner_name",
        **_ADDRESS_COLS,
        "Contact Type": "contact_type",
        "Work Phone": "work_phone",
        "Mobile Phone": "mobile_phone",
        "Work Email": "work_email",
        "Personal Email": "personal_email",
        "Work Website": "work_website",
        "Personal Website": "personal_website",
        "LinkedIn": "linkedin",
        "Facebook": "facebook",
        "Last Contacted": "last_contacted_at",
        "Created At": "created_at",
    },
    "leads": {
        **_NAME_COLS,
        "Title": "title",
        "Details": "details",
        "Value": "value",
        "Currency": "currency",
        "Company": "company_name",
        "Account": "company_name",
        "Owned By": "owner_name",
        "Source": "source",
        **_ADDRESS_COLS,
        "Work Phone": "work_phone",
        "Mobile Phone": "mobile_phone",
        "Email": "email",
        "Work Website": "work_website",
        "Personal Website": "personal_website",
        "Lead Status": "status",
        "Status": "status",
        "LinkedIn": "linkedin",
        "Facebook": "facebook",
        "Converted At": "converted_at",
        "Created At": "created_at",
    },
    "companies": {
        "Name": "name",
        "Details": "details",
        "Email Domain": "email_domain",
        **_ADDRESS_COLS,
        "Contact Type": "contact_type",
        "Owned By": "owner_name",
        "Work Phone": "work_phone",
        "Work Website": "work_website",
        "LinkedIn": "linkedin",
        "Facebook": "facebook",
        "Created At": "created_at",
    },
    "opportunities": {
        "Name": "name",
        "Details": "details",
        "Company": "company_name",
        "Primary Person Contact": "person_name",
        "Status": "status",
        "Priority": "priority",
        "Owner": "owner_name",
        "Owned By": "owner_name",
        "Close Date": "close_date",
        "Value": "value",
        "Currency": "currency",
        "Win Probability": "win_probability",
        "Pipeline": "pipeline_name",
        "Stage": "stage_name",
        "Source": "source",
        "Loss Reason": "loss_reason",
        "Created At": "created_at",
    },
}

MONEY_FIELDS = {"value"}
PERCENT_FIELDS = {"win_probability"}
DATE_FIELDS = {"close_date", "created_at", "last_contacted_at", "converted_at"}
DATETIME_KWARGS = {"created_at", "last_contacted_at", "converted_at"}

# Never settable from a row, whatever a header map says — ownership and
# lifecycle columns belong to the server.
_NEVER_IMPORTED = frozenset({"id", "org_id", "deleted_at", "updated_at", "converted_person_id"})

# The keys a commit row's `data` may carry, per type: exactly what parse_csv
# (and the Google contacts preview) produce — the header map's targets,
# including the specials _build_kwargs resolves itself (owner_name,
# company_name, pipeline_name, ...). The commit payload is client-supplied
# JSON that round-trips through the review screen, so anything outside this
# set is dropped before it gets near the model: without the allowlist a row
# could carry org_id, id or deleted_at straight into the INSERT.
IMPORTABLE_KEYS: dict[str, frozenset[str]] = {
    t: frozenset(m.values()) - _NEVER_IMPORTED for t, m in HEADER_MAPS.items()
}

# Custom fields recognized by name and created with the right control instead
# of a plain text box. Date-like fields are covered separately by sample-value
# inference in _ensure_field.
WELL_KNOWN_CF = {
    "gender": ("select", ["Male", "Female"]),
}


def _parse_money(raw: str) -> float | None:
    cleaned = raw.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_percent(raw: str) -> int | None:
    cleaned = raw.replace("%", "").strip()
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def _parse_date(raw: str) -> str | None:
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _first_pair(pairs: list[tuple[str, str]], want_type: str | None = None, used=None):
    for v, t in pairs:
        if used and v in used:
            continue
        if want_type is None or t == want_type:
            return v
    return None


def _apply_pairs(import_type: str, data: dict, buckets: dict) -> None:
    """Fold the full export's value/type pairs into the entity's columns.
    setdefault throughout: explicitly named columns always win."""
    emails = buckets.get("emails", [])
    phones = buckets.get("phones", [])
    socials = buckets.get("socials", [])
    websites = buckets.get("websites", [])

    if import_type == "people" and emails:
        work = _first_pair(emails, "work") or _first_pair(emails)
        if work:
            data.setdefault("work_email", work)
        personal = _first_pair(emails, "personal", {work}) or _first_pair(emails, None, {work})
        if personal:
            data.setdefault("personal_email", personal)
    elif import_type == "leads" and emails:
        e = _first_pair(emails, "work") or _first_pair(emails)
        if e:
            data.setdefault("email", e)

    if phones:
        if import_type in ("people", "leads"):
            work = _first_pair(phones, "work")
            mobile = _first_pair(phones, "mobile")
            if work is None:
                work = _first_pair(phones, None, {mobile} if mobile else None)
            if mobile is None:
                mobile = _first_pair(phones, None, {work} if work else None)
            if work:
                data.setdefault("work_phone", work)
            if mobile and mobile != work:
                data.setdefault("mobile_phone", mobile)
        elif import_type == "companies":
            p = _first_pair(phones, "work") or _first_pair(phones)
            if p:
                data.setdefault("work_phone", p)

    if socials and import_type in ("people", "leads", "companies"):
        li = _first_pair(socials, "linkedin") or next(
            (v for v, _ in socials if "linkedin.com" in v.lower()), None
        )
        fb = _first_pair(socials, "facebook") or next(
            (v for v, _ in socials if "facebook.com" in v.lower()), None
        )
        if li:
            data.setdefault("linkedin", li)
        if fb:
            data.setdefault("facebook", fb)

    if websites:
        work = _first_pair(websites, "work") or _first_pair(websites)
        if work:
            data.setdefault("work_website", work)
        if import_type in ("people", "leads"):
            personal = _first_pair(websites, "personal", {work})
            if personal:
                data.setdefault("personal_website", personal)


CF_NAME_MAX = 120  # custom_fields.name column width


def parse_csv(content: bytes, import_type: str) -> tuple[list[dict], list[str]]:
    """Returns (rows, unmapped_headers). Each row:
    {data, tags, custom_fields}. Handles both of Copper's shapes: the per-list
    CSV export (named columns, repeated 'Tag' columns) and the full data
    export (Email/Phone/Social/Website value+type pairs, one 'Tags' column).
    'Name cf_NNN' columns become real custom fields in both.

    A header nothing recognizes is NOT dropped: its column rides along as a
    custom field named after the header (created as a text field on commit,
    through the same path the cf_ headers take), and the header is listed in
    unmapped_headers so the review screen can say so. Whether the importing
    user may create field definitions at all is apply_definition_policy's
    call, not the parser's."""
    header_map = HEADER_MAPS[import_type]
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    try:
        headers = [h.strip() for h in next(reader)]
    except StopIteration:
        return [], []

    plan: list[tuple[str, object]] = []  # (kind, key) per column
    unmapped: list[str] = []
    for h in headers:
        if h == "Tag":
            plan.append(("tag", h))
        elif h == "Tags":
            plan.append(("tags_csv", h))
        elif h in header_map:
            plan.append(("field", header_map[h]))
        else:
            cf = CF_HEADER.match(h)
            cf_people = CF_PEOPLE_HEADER.match(h)
            pair = PAIR_HEADER.match(h)
            pair_type = PAIR_TYPE_HEADER.match(h)
            if cf:
                plan.append(("cf", cf.group(1).strip()))
            elif cf_people:
                if cf_people.group(2) == "names":
                    plan.append(("cf", cf_people.group(1).strip()))
                else:
                    plan.append(("skip", h))  # internal ids
            elif pair:
                bucket = PAIR_BUCKETS[pair.group(1)]
                slot = int((pair.group(2) or " 1").strip())
                plan.append(("pair", (bucket, slot)))
            elif pair_type:
                bucket = PAIR_BUCKETS[pair_type.group(1)]
                slot = int((pair_type.group(2) or " 1").strip())
                plan.append(("pairtype", (bucket, slot)))
            elif h in SILENT_HEADERS:
                plan.append(("skip", h))
            elif h:
                plan.append(("cf", h[:CF_NAME_MAX].strip()))
                if h not in unmapped:
                    unmapped.append(h)
            else:
                plan.append(("skip", h))

    rows = []
    for raw in reader:
        if not any(cell.strip() for cell in raw):
            continue
        raw += [""] * (len(plan) - len(raw))
        data: dict = {}
        tags: list[str] = []
        cfs: dict[str, str] = {}
        pair_values: dict[tuple[str, int], str] = {}
        pair_types: dict[tuple[str, int], str] = {}
        for (kind, key), cell in zip(plan, raw):
            cell = cell.strip()
            if not cell:
                continue
            if kind == "tag":
                tags.append(cell)
            elif kind == "tags_csv":
                tags.extend(t.strip() for t in cell.split(",") if t.strip())
            elif kind == "cf":
                # Two columns with the same name: the first value wins.
                cfs.setdefault(key, cell)
            elif kind == "pair":
                pair_values[key] = cell
            elif kind == "pairtype":
                pair_types[key] = cell.lower()
            elif kind == "field":
                if key in MONEY_FIELDS:
                    parsed = _parse_money(cell)
                elif key in PERCENT_FIELDS:
                    parsed = _parse_percent(cell)
                elif key in DATE_FIELDS:
                    parsed = _parse_date(cell)
                else:
                    parsed = cell
                if parsed is not None:
                    data[key] = parsed
        if pair_values:
            buckets: dict[str, list[tuple[str, str]]] = {}
            for (bucket, slot), value in sorted(pair_values.items(), key=lambda x: x[0][1]):
                buckets.setdefault(bucket, []).append((value, pair_types.get((bucket, slot), "")))
            _apply_pairs(import_type, data, buckets)
        if data or tags or cfs:
            rows.append({"data": data, "tags": tags, "custom_fields": cfs})
    return rows, unmapped


async def apply_definition_policy(
    db: AsyncSession, user: User, import_type: str, rows: list[dict], unmapped: list[str]
) -> dict:
    """Preview-time half of the import's definition policy; returns the header
    report for the preview response.

    Creating NEW custom-field or pipeline/stage definitions is an admin
    action everywhere else in the app, and an import is no back door: a
    non-admin's columns still fill fields that already exist, but a column
    that would need a new field is removed from the rows here — and named in
    discarded_headers, so the review screen can warn before anything is
    committed instead of the data quietly going missing. The commit side
    enforces the same rule on its own (_CommitContext.can_define); this
    function is about telling the user the truth up front.

      unmapped_headers     unrecognized headers whose values WILL be kept, as
                           custom fields
      discarded_headers    columns (unrecognized headers and cf_ fields alike)
                           whose values will NOT be imported
      discarded_pipelines  pipelines / stages named in the file that don't
                           exist and won't be created (the deals still import,
                           without them)"""
    if user.is_admin:
        return {
            "unmapped_headers": unmapped,
            "discarded_headers": [],
            "discarded_pipelines": [],
        }

    entity_type = IMPORT_TYPES[import_type]
    existing = {
        name.lower()
        for (name,) in await db.execute(
            select(CustomField.name).where(
                CustomField.org_id == user.org_id, CustomField.entity_type == entity_type
            )
        )
    }
    discarded: list[str] = []
    for row in rows:
        cfs = row.get("custom_fields") or {}
        for name in [n for n in cfs if n.lower() not in existing]:
            if name not in discarded:
                discarded.append(name)
            del cfs[name]
    gone = {d.lower() for d in discarded}
    kept = [h for h in unmapped if h[:CF_NAME_MAX].strip().lower() not in gone]

    discarded_pipelines: list[str] = []
    if import_type == "opportunities":
        stages_by_pipeline: dict[str, set[str]] = {}
        for pname, sname in await db.execute(
            select(Pipeline.name, Stage.name)
            .outerjoin(Stage, Stage.pipeline_id == Pipeline.id)
            .where(Pipeline.org_id == user.org_id)
        ):
            known = stages_by_pipeline.setdefault(pname.lower().strip(), set())
            if sname:
                known.add(sname.lower().strip())
        for row in rows:
            pname = (row["data"].get("pipeline_name") or "").strip()
            sname = (row["data"].get("stage_name") or "").strip()
            if not pname:
                continue
            known = stages_by_pipeline.get(pname.lower())
            if known is None:
                label = pname
            elif sname and sname.lower() not in known:
                label = f"{pname} / {sname}"
            else:
                continue
            if label not in discarded_pipelines:
                discarded_pipelines.append(label)
    return {
        "unmapped_headers": kept,
        "discarded_headers": discarded,
        "discarded_pipelines": discarded_pipelines,
    }


def _full_name(data: dict) -> str:
    return " ".join(
        filter(None, [data.get("first_name", "").strip(), data.get("last_name", "").strip()])
    )


async def find_duplicates(
    db: AsyncSession, org_id: uuid.UUID, import_type: str, rows: list[dict]
) -> int:
    """Annotates rows in place with `duplicates` (existing-record matches) and
    `intra_file_duplicate_of` (index of an earlier identical row). Uses bulk
    lookups so a 19k-row file stays fast."""
    email_map: dict[str, tuple[str, str]] = {}
    name_map: dict[str, tuple[str, str]] = {}
    domain_map: dict[str, tuple[str, str]] = {}

    if import_type == "people":
        emails = {
            e.lower()
            for r in rows
            for e in (r["data"].get("work_email"), r["data"].get("personal_email"))
            if e
        }
        # asyncpg caps bind parameters at 32767 — a full Copper book easily
        # exceeds that in one IN().
        email_list = sorted(emails)
        for i in range(0, len(email_list), 5000):
            chunk = email_list[i : i + 5000]
            found = await db.execute(
                select(Person.id, Person.first_name, Person.last_name, Person.work_email,
                       Person.personal_email).where(
                    Person.org_id == org_id,
                    Person.deleted_at.is_(None),
                    func.lower(Person.work_email).in_(chunk)
                    | func.lower(Person.personal_email).in_(chunk),
                )
            )
            for pid, first, last, work, personal in found:
                label = " ".join(filter(None, [first, last]))
                for e in (work, personal):
                    if e and e.lower() in emails:
                        email_map[e.lower()] = (str(pid), label)
        names = await db.execute(
            select(
                Person.id,
                func.lower(
                    func.coalesce(Person.first_name, "") + " " + func.coalesce(Person.last_name, "")
                ),
                Person.first_name,
                Person.last_name,
            ).where(Person.org_id == org_id, Person.deleted_at.is_(None))
        )
        for pid, key, first, last in names:
            name_map[key.strip()] = (str(pid), " ".join(filter(None, [first, last])))
    elif import_type == "leads":
        emails = {r["data"]["email"].lower() for r in rows if r["data"].get("email")}
        email_list = sorted(emails)
        for i in range(0, len(email_list), 5000):
            chunk = email_list[i : i + 5000]
            found = await db.execute(
                select(Lead.id, Lead.first_name, Lead.last_name, Lead.email).where(
                    Lead.org_id == org_id,
                    Lead.deleted_at.is_(None),
                    func.lower(Lead.email).in_(chunk),
                )
            )
            for lid, first, last, email in found:
                email_map[email.lower()] = (str(lid), " ".join(filter(None, [first, last])))
        names = await db.execute(
            select(
                Lead.id,
                func.lower(
                    func.coalesce(Lead.first_name, "") + " " + func.coalesce(Lead.last_name, "")
                ),
                Lead.first_name,
                Lead.last_name,
            ).where(Lead.org_id == org_id, Lead.deleted_at.is_(None))
        )
        for lid, key, first, last in names:
            name_map[key.strip()] = (str(lid), " ".join(filter(None, [first, last])))
    elif import_type == "companies":
        found = await db.execute(
            select(Company.id, Company.name, Company.email_domain).where(
                Company.org_id == org_id, Company.deleted_at.is_(None)
            )
        )
        for cid, name, domain in found:
            name_map[name.lower().strip()] = (str(cid), name)
            if domain:
                domain_map[domain.lower()] = (str(cid), name)
    elif import_type == "opportunities":
        found = await db.execute(
            select(Opportunity.id, Opportunity.name).where(
                Opportunity.org_id == org_id, Opportunity.deleted_at.is_(None)
            )
        )
        for oid, name in found:
            name_map[name.lower().strip()] = (str(oid), name)

    seen: dict[str, int] = {}
    duplicates_found = 0
    for i, row in enumerate(rows):
        data = row["data"]
        dups = []
        keys = []
        if import_type == "people":
            for e in (data.get("work_email"), data.get("personal_email")):
                if e:
                    keys.append(("email", e.lower()))
            if _full_name(data):
                keys.append(("name", _full_name(data).lower()))
        elif import_type == "leads":
            if data.get("email"):
                keys.append(("email", data["email"].lower()))
            if _full_name(data):
                keys.append(("name", _full_name(data).lower()))
        elif import_type == "companies":
            if data.get("name"):
                keys.append(("name", data["name"].lower().strip()))
            if data.get("email_domain"):
                keys.append(("domain", data["email_domain"].lower()))
        elif import_type == "opportunities":
            if data.get("name"):
                keys.append(("name", data["name"].lower().strip()))

        matched_ids = set()
        for kind, key in keys:
            source = {"email": email_map, "name": name_map, "domain": domain_map}[kind]
            hit = source.get(key)
            if hit and hit[0] not in matched_ids:
                matched_ids.add(hit[0])
                dups.append({"id": hit[0], "label": hit[1], "reason": kind})

        row["duplicates"] = dups
        row["intra_file_duplicate_of"] = None
        for kind, key in keys:
            file_key = f"{kind}:{key}"
            if file_key in seen:
                row["intra_file_duplicate_of"] = seen[file_key]
                break
        for kind, key in keys:
            seen.setdefault(f"{kind}:{key}", i)
        if dups or row["intra_file_duplicate_of"] is not None:
            duplicates_found += 1
    return duplicates_found


MODEL_BY_TYPE = {
    "people": Person,
    "leads": Lead,
    "companies": Company,
    "opportunities": Opportunity,
}

OPP_STATUS = {"open": "open", "won": "won", "lost": "lost", "abandoned": "abandoned"}


class _CommitContext:
    """Per-commit caches so 19k rows don't issue a lookup per cell."""

    def __init__(self):
        self.users: dict[str, uuid.UUID] = {}
        self.companies: dict[str, uuid.UUID] = {}
        # lower("first last") -> id, None for a name that matched nobody
        self.persons: dict[str, uuid.UUID | None] = {}
        self.pipelines: dict[str, uuid.UUID] = {}
        self.stages: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
        self.fields: dict[str, uuid.UUID] = {}
        self.field_types: dict[uuid.UUID, str] = {}
        self.field_objs: dict[uuid.UUID, CustomField] = {}
        self.fields_created: list[str] = []
        self.tags: dict[str, uuid.UUID] = {}
        # May this import create custom-field / pipeline / stage definitions?
        # Admin-only, like the settings screens that manage them. Off by
        # default so a caller that forgets to decide gets the safe answer.
        self.can_define: bool = False


async def _tag_id(db: AsyncSession, org_id: uuid.UUID, ctx: _CommitContext, name: str) -> uuid.UUID:
    if name not in ctx.tags:
        ctx.tags[name] = (await get_or_create_tag(db, org_id, name)).id
    return ctx.tags[name]


async def _load_users(db: AsyncSession, org_id: uuid.UUID, ctx: _CommitContext) -> None:
    rows = await db.execute(select(User.id, User.display_name).where(User.org_id == org_id))
    for uid, name in rows:
        ctx.users[name.lower()] = uid


async def _load_persons(db: AsyncSession, org_id: uuid.UUID, ctx: _CommitContext) -> None:
    # Opportunity rows name their primary contact in free text. One pass over
    # the org's people beats a lower(first || ' ' || last) scan per row — the
    # key here is built exactly like the SQL expression _resolve_person
    # compares against, unstripped, so the two agree.
    rows = await db.execute(
        select(Person.id, Person.first_name, Person.last_name).where(
            Person.org_id == org_id, Person.deleted_at.is_(None)
        )
    )
    for pid, first, last in rows:
        ctx.persons.setdefault(f"{first or ''} {last or ''}".lower(), pid)


async def _resolve_company(
    db: AsyncSession, org_id: uuid.UUID, ctx: _CommitContext, name: str
) -> uuid.UUID:
    key = name.lower().strip()
    if key in ctx.companies:
        return ctx.companies[key]
    company = (
        await db.execute(
            select(Company).where(
                Company.org_id == org_id,
                func.lower(Company.name) == key,
                Company.deleted_at.is_(None),
            )
        )
    ).scalars().first()
    if company is None:
        company = Company(org_id=org_id, name=name.strip())
        db.add(company)
        await db.flush()
    ctx.companies[key] = company.id
    return company.id


async def _resolve_pipeline_stage(
    db: AsyncSession,
    org_id: uuid.UUID,
    ctx: _CommitContext,
    pipeline_name: str | None,
    stage_name: str | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    pipeline_id = None
    if pipeline_name:
        key = pipeline_name.lower().strip()
        pipeline_id = ctx.pipelines.get(key)
        if pipeline_id is None:
            p = (
                await db.execute(
                    select(Pipeline).where(
                        Pipeline.org_id == org_id, func.lower(Pipeline.name) == key
                    )
                )
            ).scalars().first()
            if p is None and not ctx.can_define:
                return None, None
            if p is None:
                max_pos = (
                    await db.execute(
                        select(func.coalesce(func.max(Pipeline.position), -1)).where(
                            Pipeline.org_id == org_id
                        )
                    )
                ).scalar_one()
                p = Pipeline(org_id=org_id, name=pipeline_name.strip(), position=max_pos + 1)
                db.add(p)
                await db.flush()
            pipeline_id = p.id
            ctx.pipelines[key] = pipeline_id
    stage_id = None
    if pipeline_id and stage_name:
        skey = (pipeline_id, stage_name.lower().strip())
        stage_id = ctx.stages.get(skey)
        if stage_id is None:
            s = (
                await db.execute(
                    select(Stage).where(
                        Stage.pipeline_id == pipeline_id,
                        func.lower(Stage.name) == stage_name.lower().strip(),
                    )
                )
            ).scalars().first()
            if s is None and not ctx.can_define:
                return pipeline_id, None
            if s is None:
                max_pos = (
                    await db.execute(
                        select(func.coalesce(func.max(Stage.position), -1)).where(
                            Stage.pipeline_id == pipeline_id
                        )
                    )
                ).scalar_one()
                s = Stage(pipeline_id=pipeline_id, name=stage_name.strip(), position=max_pos + 1)
                db.add(s)
                await db.flush()
            stage_id = s.id
            ctx.stages[skey] = stage_id
    return pipeline_id, stage_id


async def _resolve_person(
    db: AsyncSession, org_id: uuid.UUID, ctx: _CommitContext, name: str
) -> uuid.UUID | None:
    key = name.lower().strip()
    if key in ctx.persons:
        return ctx.persons[key]
    # Not preloaded (added since the import started, or simply unknown) —
    # ask once and remember the answer either way, since the same name tends
    # to recur across an export.
    row = (
        await db.execute(
            select(Person.id).where(
                Person.org_id == org_id,
                Person.deleted_at.is_(None),
                func.lower(
                    func.coalesce(Person.first_name, "") + " " + func.coalesce(Person.last_name, "")
                )
                == key,
            )
        )
    ).scalars().first()
    ctx.persons[key] = row
    return row


async def _ensure_field(
    db: AsyncSession, org_id: uuid.UUID, ctx: _CommitContext, entity_type: str, name: str,
    sample_value: str | None = None,
) -> uuid.UUID | None:
    """The id of the custom field called `name` (case-insensitively), created
    if missing — or None when it is missing and this import may not create
    definitions, in which case the caller drops the value."""
    key = f"{entity_type}:{name.lower()}"
    if key in ctx.fields:
        return ctx.fields[key]
    field = (
        await db.execute(
            select(CustomField).where(
                CustomField.org_id == org_id,
                CustomField.entity_type == entity_type,
                func.lower(CustomField.name) == name.lower(),
            )
        )
    ).scalars().first()
    if field is None and not ctx.can_define:
        return None
    if field is None:
        max_pos = (
            await db.execute(
                select(func.coalesce(func.max(CustomField.position), -1)).where(
                    CustomField.org_id == org_id, CustomField.entity_type == entity_type
                )
            )
        ).scalar_one()
        known = WELL_KNOWN_CF.get(name.lower())
        if known:
            field_type, options = known
        else:
            field_type = "date" if sample_value and _parse_date(str(sample_value)) else "text"
            options = None
        field = CustomField(
            org_id=org_id, entity_type=entity_type, name=name, position=max_pos + 1,
            field_type=field_type, options=options,
        )
        db.add(field)
        await db.flush()
        ctx.fields_created.append(name)
    ctx.fields[key] = field.id
    ctx.field_types[field.id] = field.field_type
    ctx.field_objs[field.id] = field
    return field.id


def _coerce_cf_value(ctx: _CommitContext, field_id: uuid.UUID, value) -> str:
    field_type = ctx.field_types.get(field_id)
    if field_type == "date":
        parsed = _parse_date(str(value))
        if parsed:
            value = parsed
    elif field_type == "checkbox":
        return canonical_checkbox(value) or "false"
    elif field_type in ("select", "dropdown"):
        # Match the option's canonical casing; unknown values become new
        # options rather than silently vanishing from the dropdown.
        field = ctx.field_objs[field_id]
        options = list(field.options or [])
        canon = next((o for o in options if o.lower() == str(value).strip().lower()), None)
        if canon is not None:
            value = canon
        else:
            value = str(value).strip()
            field.options = options + [value]
    return str(value)


async def _resolve_cf_values(
    db: AsyncSession, org_id: uuid.UUID, ctx: _CommitContext, entity_type: str, cfs: dict
) -> dict[uuid.UUID, str]:
    """One row's custom values keyed by FIELD ID, ready to store. Keying by id
    is the point: "Color" and "color" are the same field (names match
    case-insensitively), and two entries for one field would be two inserts
    of the same primary key. The first value wins. Values for fields that
    don't exist and may not be created are dropped."""
    out: dict[uuid.UUID, str] = {}
    for name, value in (cfs or {}).items():
        if value in (None, ""):
            continue
        name = str(name).strip()[:CF_NAME_MAX].strip()
        if not name:
            continue
        field_id = await _ensure_field(db, org_id, ctx, entity_type, name, sample_value=value)
        if field_id is None or field_id in out:
            continue
        out[field_id] = _coerce_cf_value(ctx, field_id, value)
    return out


async def _set_cf_values(
    db: AsyncSession,
    org_id: uuid.UUID,
    ctx: _CommitContext,
    entity_type: str,
    entity_id: uuid.UUID,
    cfs: dict,
    only_if_missing: bool = False,
) -> None:
    values = await _resolve_cf_values(db, org_id, ctx, entity_type, cfs)
    for field_id, value in values.items():
        existing = (
            await db.execute(
                select(CustomFieldValue).where(
                    CustomFieldValue.field_id == field_id,
                    CustomFieldValue.entity_id == entity_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if not only_if_missing:
                existing.value = value
        else:
            db.add(
                CustomFieldValue(
                    field_id=field_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    value=value,
                )
            )
    # Autoflush is off, so the lookup above only sees what has reached the
    # database. Flush now: the next row in this chunk may merge into the very
    # same record, and must find these values instead of inserting the same
    # (field, record) key a second time.
    await db.flush()


def _to_datetime(iso_date: str) -> datetime:
    return datetime.fromisoformat(iso_date).replace(tzinfo=timezone.utc)


def _importable(import_type: str, data: dict) -> dict:
    """The row's data reduced to the keys this import type may set."""
    allowed = IMPORTABLE_KEYS[import_type]
    return {key: value for key, value in data.items() if key in allowed}


async def _build_kwargs(
    db: AsyncSession, org_id: uuid.UUID, ctx: _CommitContext, import_type: str, data: dict
) -> dict:
    # Allowlist first — both the create and merge paths come through here.
    data = _importable(import_type, data)
    kwargs: dict = {}

    owner_name = data.pop("owner_name", None)
    if owner_name:
        kwargs["owner_id"] = ctx.users.get(owner_name.lower().strip())

    # Timestamp columns need real datetimes, not the ISO date strings parsing
    # produced.
    extra_dates = {
        key: _to_datetime(v) for key in DATETIME_KWARGS if (v := data.pop(key, None))
    }
    created_at = extra_dates.pop("created_at", None)

    if import_type == "people":
        company_name = data.pop("company_name", None)
        if company_name:
            kwargs["company_id"] = await _resolve_company(db, org_id, ctx, company_name)
    elif import_type == "opportunities":
        company_name = data.pop("company_name", None)
        if company_name:
            kwargs["company_id"] = await _resolve_company(db, org_id, ctx, company_name)
        person_name = data.pop("person_name", None)
        if person_name:
            kwargs["primary_person_id"] = await _resolve_person(db, org_id, ctx, person_name)
        pipeline_id, stage_id = await _resolve_pipeline_stage(
            db, org_id, ctx, data.pop("pipeline_name", None), data.pop("stage_name", None)
        )
        kwargs["pipeline_id"] = pipeline_id
        kwargs["stage_id"] = stage_id
        status = (data.pop("status", "") or "").lower().strip()
        kwargs["status"] = OPP_STATUS.get(status, "open")
        close_date = data.pop("close_date", None)
        if close_date:
            kwargs["close_date"] = datetime.fromisoformat(close_date).date()
        if kwargs["status"] in CLOSED_OPPORTUNITY_STATUSES:
            # Reports date a win/loss by closed_at. An imported deal closed
            # whenever the source system says it did — not at import time.
            kwargs["closed_at"] = (
                _to_datetime(close_date) if close_date else created_at or utcnow()
            )
    elif import_type == "leads":
        # "new", "NEW" and "New" are one status; store the canonical casing.
        status = canonical_lead_status(data.pop("status", None))
        if status:
            kwargs["status"] = status

    model = MODEL_BY_TYPE[import_type]
    columns = model.__table__.columns
    for key, value in data.items():
        if key in columns and key not in _NEVER_IMPORTED:
            # Real exports carry monster URLs and titles; hard-fail on one row
            # would kill the whole chunk.
            length = getattr(columns[key].type, "length", None)
            if isinstance(value, str) and length and len(value) > length:
                value = value[:length]
            kwargs[key] = value
    for key, value in extra_dates.items():
        if key in columns:
            kwargs[key] = value
    if created_at:
        kwargs["created_at"] = created_at
    return kwargs


async def _commit_chunk(
    db: AsyncSession,
    org_id: uuid.UUID,
    ctx: _CommitContext,
    import_type: str,
    rows: list[dict],
) -> tuple[int, int, int]:
    """Process one chunk of payload rows with set-based writes: new records,
    their tags, and their custom values each land as bulk inserts instead of
    a handful of round trips per row."""
    entity_type = IMPORT_TYPES[import_type]
    model = MODEL_BY_TYPE[import_type]
    created = merged = skipped = 0
    create_rows: list[dict] = []
    tag_rows: list[dict] = []
    cf_rows: list[dict] = []

    for row in rows:
        action = row.get("action") or "create"
        merge_id = row.get("merge_id")
        if action == "skip":
            skipped += 1
            continue
        if action == "merge" and merge_id:
            existing = (
                await db.execute(
                    select(model).where(
                        model.id == uuid.UUID(str(merge_id)), model.org_id == org_id
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                skipped += 1
                continue
            kwargs = await _build_kwargs(db, org_id, ctx, import_type, row.get("data") or {})
            kwargs.pop("created_at", None)
            # A merge only fills blanks, so it never changes an existing
            # deal's status — and must not stamp a close time onto it either.
            kwargs.pop("closed_at", None)
            for key, value in kwargs.items():
                if value not in (None, "") and getattr(existing, key, None) in (None, ""):
                    setattr(existing, key, value)
            if row.get("tags"):
                await add_tags(db, org_id, entity_type, existing.id, row["tags"])
                # Pending tag links are invisible to the next row's lookup
                # (autoflush is off) — and that row may target this record.
                await db.flush()
            if row.get("custom_fields"):
                await _set_cf_values(
                    db, org_id, ctx, entity_type, existing.id, row["custom_fields"],
                    only_if_missing=True,
                )
            merged += 1
            continue

        kwargs = await _build_kwargs(db, org_id, ctx, import_type, row.get("data") or {})
        rid = uuid.uuid4()
        create_rows.append({"id": rid, "org_id": org_id, **kwargs})
        for name in dict.fromkeys(t.strip() for t in (row.get("tags") or []) if t and t.strip()):
            tag_rows.append(
                {
                    "tag_id": await _tag_id(db, org_id, ctx, name),
                    "entity_type": entity_type,
                    "entity_id": rid,
                }
            )
        values = await _resolve_cf_values(
            db, org_id, ctx, entity_type, row.get("custom_fields") or {}
        )
        for field_id, value in values.items():
            cf_rows.append(
                {
                    "field_id": field_id,
                    "entity_type": entity_type,
                    "entity_id": rid,
                    "value": value,
                }
            )
        created += 1

    # executemany needs uniform keys — group by key set (defaults fill the rest)
    by_keys: dict[frozenset, list[dict]] = {}
    for r in create_rows:
        by_keys.setdefault(frozenset(r), []).append(r)
    for group in by_keys.values():
        await db.execute(insert(model), group)
    if tag_rows:
        await db.execute(insert(EntityTag), tag_rows)
    if cf_rows:
        await db.execute(insert(CustomFieldValue), cf_rows)
    return created, merged, skipped


async def run_import_job(job_id: uuid.UUID) -> None:
    """Background worker: processes a committed import in chunks, committing
    progress as it goes so a restart resumes instead of restarting."""
    from app.db import SessionLocal

    async with SessionLocal() as db:
        job = await db.get(ImportJob, job_id)
        if job is None or job.status != "running":
            return
        ctx = _CommitContext()
        # Definitions (custom fields, pipelines, stages) are admin-managed;
        # the import runs with the rights of whoever committed it.
        importer = await db.get(User, job.user_id) if job.user_id else None
        ctx.can_define = bool(importer and importer.is_admin and importer.is_active)
        await _load_users(db, job.org_id, ctx)
        if job.import_type == "opportunities":
            await _load_persons(db, job.org_id, ctx)
        try:
            payload = job.payload or []
            while job.processed < job.total:
                chunk = payload[job.processed : job.processed + COMMIT_CHUNK]
                if not chunk:
                    break
                c, m, s = await _commit_chunk(db, job.org_id, ctx, job.import_type, chunk)
                job.processed += len(chunk)
                job.created_count += c
                job.merged_count += m
                job.skipped_count += s
                job.fields_created = sorted(set(job.fields_created or []) | set(ctx.fields_created))
                job.updated_at = utcnow()
                await db.commit()
            await log_activity(
                db, job.org_id, None, None, "import_completed", job.user_id,
                {
                    "type": job.import_type,
                    "created": job.created_count,
                    "merged": job.merged_count,
                    "skipped": job.skipped_count,
                },
            )
            job.status = "done"
            job.payload = []  # the parsed rows are dead weight once imported
            job.updated_at = utcnow()
            await db.commit()
            log.info(
                "import %s done: %s created, %s merged, %s skipped",
                job_id, job.created_count, job.merged_count, job.skipped_count,
            )
        except Exception as exc:
            log.exception("import job %s failed", job_id)
            await db.rollback()
            job.status = "failed"
            job.error = str(exc)[:1000]
            job.updated_at = utcnow()
            await db.commit()


async def resume_interrupted_imports() -> None:
    """Called at startup: any job still marked running was cut off by a
    restart — pick it up from its committed offset."""
    from app.db import SessionLocal
    from app.services.background import spawn

    async with SessionLocal() as db:
        stale = (
            await db.execute(select(ImportJob.id).where(ImportJob.status == "running"))
        ).scalars().all()
    for job_id in stale:
        log.info("resuming interrupted import %s", job_id)
        spawn(run_import_job(job_id))
