"""Which email addresses and phone numbers belong to which record.

Synced mail, calendar events, calls and texts are stored by address, not by
record, so "whose is this?" is answered by normalizing the addresses a record
carries and comparing. This module is the only place that knows

  * how an address is normalized (normalize_email, normalize_phone),
  * which columns of a person, lead or company hold addresses, and
  * who wins when two records list the same one.

Everything else asks here: the sync jobs (is this address a known contact?),
the feed and email search (which record does this item point at?), the
per-record timelines (which addresses does this record own?), and the
interaction counters.
"""

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import NamedTuple

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Lead, Person

# ---- normalization ----


def normalize_email(addr: str) -> str:
    """Canonical form for matching. Gmail ignores dots and +suffixes in the
    local part, so `m.r.w+x@gmail.com` and `mrw@googlemail.com` are the same
    inbox — match them as one."""
    addr = (addr or "").lower().strip()
    local, _, domain = addr.partition("@")
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
        return f"{local}@gmail.com"
    return addr


def normalize_phone(raw: str | None, default_country: str = "1") -> str | None:
    """Loose E.164 normalization. US-centric default: 10 digits get +1."""
    if not raw:
        return None
    digits = re.sub(r"[^\d+]", "", str(raw))
    if not digits:
        return None
    if digits.startswith("+"):
        return "+" + re.sub(r"\D", "", digits)
    digits = re.sub(r"\D", "", digits)
    if len(digits) == 10:
        return f"+{default_country}{digits}"
    if len(digits) == 11 and digits.startswith(default_country):
        return f"+{digits}"
    if len(digits) > 11:
        return f"+{digits}"
    return None  # too short to be a real number (extensions etc.)


# ---- which columns hold addresses ----

# In slot order: the first is the record's primary address of that kind.
EMAIL_FIELDS: dict[str, tuple[str, ...]] = {
    "person": ("work_email", "personal_email"),
    "lead": ("email",),
}
PHONE_FIELDS: dict[str, tuple[str, ...]] = {
    "person": ("work_phone", "mobile_phone"),
    "lead": ("work_phone", "mobile_phone"),
}
# Record types that have a correspondence timeline at all. A company has no
# addresses of its own: it answers for its email domain and for its people.
CORRESPONDENT_TYPES = ("person", "lead", "company")


def address_fields(entity_type: str) -> tuple[str, ...]:
    """Every column whose edit changes which synced events belong to a record."""
    return EMAIL_FIELDS.get(entity_type, ()) + PHONE_FIELDS.get(entity_type, ())


_MODELS = {"person": Person, "lead": Lead}


def email_columns(entity_type: str) -> tuple:
    """A record type's email columns, primary first (for SQL that must look
    at all of them, like duplicate detection)."""
    model = _MODELS[entity_type]
    return tuple(getattr(model, f) for f in EMAIL_FIELDS[entity_type])


def _dedupe(values) -> list[str]:
    out: list[str] = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


def raw_emails(entity_type: str, obj) -> list[str]:
    """The record's email addresses as typed, primary first."""
    return [e for f in EMAIL_FIELDS.get(entity_type, ()) if (e := getattr(obj, f, None))]


def primary_email(entity_type: str, obj) -> str | None:
    emails = raw_emails(entity_type, obj)
    return emails[0] if emails else None


def record_emails(entity_type: str, obj) -> list[str]:
    """The record's own email addresses, normalized, primary first. `obj` is
    anything with the columns as attributes (a model instance or a row)."""
    return _dedupe(normalize_email(e) for e in raw_emails(entity_type, obj))


def record_numbers(entity_type: str, obj) -> list[str]:
    """The record's own phone numbers, normalized, primary first."""
    return _dedupe(
        normalize_phone(getattr(obj, f, None)) for f in PHONE_FIELDS.get(entity_type, ())
    )


# ---- one record's contact points ----


@dataclass
class ContactPoints:
    """Everything a record answers for. For a person or lead that is its own
    addresses. For a company it is the union of its people's addresses, plus
    the company's email domain and the ids of those people (events logged
    directly on one of them belong on the company's timeline too)."""

    emails: list[str] = field(default_factory=list)
    numbers: list[str] = field(default_factory=list)
    domain: str | None = None
    person_ids: list[uuid.UUID] = field(default_factory=list)


async def company_people(db: AsyncSession, org_id: uuid.UUID, company_id: uuid.UUID):
    """Non-deleted people linked to a company, with just the columns needed to
    roll their correspondence up onto the company. One query."""
    cols = [getattr(Person, f) for f in address_fields("person")]
    rows = await db.execute(
        select(Person.id, *cols).where(
            Person.org_id == org_id,
            Person.company_id == company_id,
            Person.deleted_at.is_(None),
        )
    )
    return rows.all()


async def contact_points(
    db: AsyncSession, org_id: uuid.UUID, entity_type: str, obj
) -> ContactPoints:
    if entity_type == "company":
        people = await company_people(db, org_id, obj.id)
        return ContactPoints(
            emails=_dedupe(e for p in people for e in record_emails("person", p)),
            numbers=_dedupe(n for p in people for n in record_numbers("person", p)),
            domain=(obj.email_domain or "").lower().strip() or None,
            person_ids=[p.id for p in people],
        )
    return ContactPoints(
        emails=record_emails(entity_type, obj),
        numbers=record_numbers(entity_type, obj),
    )


# ---- the org-wide index: address -> record ----


class ContactRef(NamedTuple):
    entity_type: str  # person | lead
    entity_id: uuid.UUID
    label: str


@dataclass
class ContactIndex:
    """Every live contact address in an org, each pointing at one record.

    Precedence when two records list the same address: a person beats a lead,
    and between two records of the same type the older one wins (created_at,
    then id). That is one fixed answer; a shared phone line still counts for
    everyone who lists it — see people_with_numbers."""

    by_email: dict[str, ContactRef] = field(default_factory=dict)
    by_number: dict[str, ContactRef] = field(default_factory=dict)


async def load_contact_index(db: AsyncSession, org_id: uuid.UUID) -> ContactIndex:
    """Read the index fresh. Two skinny scans (people, leads); the sync jobs
    call this once per pass, not per message."""
    index = ContactIndex()
    for entity_type, model in _MODELS.items():  # people first: they win ties
        cols = [getattr(model, f) for f in address_fields(entity_type)]
        rows = await db.execute(
            select(model.id, model.first_name, model.last_name, *cols)
            .where(model.org_id == org_id, model.deleted_at.is_(None))
            .order_by(model.created_at, model.id)
        )
        for row in rows:
            ref = ContactRef(
                entity_type, row.id, " ".join(filter(None, [row.first_name, row.last_name]))
            )
            for email in record_emails(entity_type, row):
                index.by_email.setdefault(email, ref)
            for number in record_numbers(entity_type, row):
                index.by_number.setdefault(number, ref)
    return index


# Read paths (feed, email search, sync status) label items with matching
# records on every request. Building the index reads the whole contact book,
# so those share one copy briefly: a slightly stale chip is invisible, a full
# scan per page view is not.
_CACHE_TTL_SECONDS = 120
_cache: dict[uuid.UUID, tuple[float, ContactIndex]] = {}


async def cached_contact_index(db: AsyncSession, org_id: uuid.UUID) -> ContactIndex:
    hit = _cache.get(org_id)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    index = await load_contact_index(db, org_id)
    _cache[org_id] = (time.monotonic(), index)
    return index


def clear_cache() -> None:
    _cache.clear()


# ---- shared lines ----


async def people_with_numbers(
    db: AsyncSession, org_id: uuid.UUID, numbers: set[str]
) -> set[uuid.UUID]:
    """Every live person who lists any of these (normalized) numbers. Two
    people can share a line — a front desk, a couple — and an event on that
    number counts for each of them, so each needs recomputing when one is
    stored.

    Numbers are stored as typed, so matching means normalizing each one: one
    read of the org's numbers, always fresh."""
    numbers = {n for n in numbers if n}
    if not numbers:
        return set()
    await db.flush()
    cols = [getattr(Person, f) for f in PHONE_FIELDS["person"]]
    rows = await db.execute(
        select(Person.id, *cols).where(
            Person.org_id == org_id,
            Person.deleted_at.is_(None),
            or_(*[c.is_not(None) for c in cols]),
        )
    )
    return {
        row.id for row in rows if numbers.intersection(record_numbers("person", row))
    }


async def people_with_number(
    db: AsyncSession, org_id: uuid.UUID, number: str | None
) -> set[uuid.UUID]:
    return await people_with_numbers(db, org_id, {number} if number else set())
