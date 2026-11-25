"""Address matching: normalization, which record owns a shared address, and
what a company answers for."""

from datetime import timedelta

from app.models import utcnow
from app.services import contacts

from .conftest import make_company, make_lead, make_person


def test_gmail_addresses_fold_to_one_inbox():
    assert contacts.normalize_email(" M.R.W+news@GoogleMail.com ") == "mrw@gmail.com"
    assert contacts.normalize_email("First.Last@Example.com") == "first.last@example.com"


def test_phone_numbers_normalize_to_e164():
    assert contacts.normalize_phone("(555) 123-4567") == "+15551234567"
    assert contacts.normalize_phone("1 555 123 4567") == "+15551234567"
    assert contacts.normalize_phone("+44 20 7946 0958") == "+442079460958"
    assert contacts.normalize_phone("x204") is None


async def test_shared_address_goes_to_the_person_then_the_oldest(world, db_session):
    now = utcnow()
    await make_lead(world.org, email="shared@example.com", created_at=now - timedelta(days=9))
    newer = await make_person(
        world.org, first_name="New", work_email="shared@example.com", work_phone="555-123-4567",
        created_at=now - timedelta(days=1),
    )
    older = await make_person(
        world.org, first_name="Old", personal_email="Shared@Example.com",
        mobile_phone="(555) 123-4567", created_at=now - timedelta(days=5),
    )
    async with db_session() as db:
        index = await contacts.load_contact_index(db, world.org.id)
        both = await contacts.people_with_number(db, world.org.id, "+15551234567")

    # A person beats a lead whatever their ages; of two people, the older.
    assert index.by_email["shared@example.com"].entity_type == "person"
    assert index.by_email["shared@example.com"].entity_id == older.id
    assert index.by_number["+15551234567"].entity_id == older.id
    assert index.by_email["shared@example.com"].label == "Old Person"
    # The line itself still counts for everyone who lists it.
    assert both == {newer.id, older.id}


async def test_trashed_records_are_not_in_the_index(world, db_session):
    await make_person(world.org, work_email="gone@example.com", deleted_at=utcnow())
    async with db_session() as db:
        index = await contacts.load_contact_index(db, world.org.id)
    assert "gone@example.com" not in index.by_email


async def test_a_company_answers_for_its_domain_and_its_people(world, db_session):
    company = await make_company(world.org, email_domain=" Globex.com ")
    a = await make_person(
        world.org, company_id=company.id, work_email="a@globex.com",
        personal_email="a.b+x@gmail.com", mobile_phone="555-000-1111",
    )
    b = await make_person(world.org, company_id=company.id, work_phone="555-000-2222")
    await make_person(world.org, work_email="elsewhere@example.com")
    lead = await make_lead(world.org, email="lee@example.com", work_phone="555-000-3333")

    async with db_session() as db:
        of_company = await contacts.contact_points(db, world.org.id, "company", company)
        of_person = await contacts.contact_points(db, world.org.id, "person", a)
        of_lead = await contacts.contact_points(db, world.org.id, "lead", lead)

    assert of_company.domain == "globex.com"
    assert set(of_company.emails) == {"a@globex.com", "ab@gmail.com"}
    assert set(of_company.numbers) == {"+15550001111", "+15550002222"}
    assert set(of_company.person_ids) == {a.id, b.id}
    assert of_person.emails == ["a@globex.com", "ab@gmail.com"]
    assert of_person.person_ids == [] and of_person.domain is None
    assert of_lead.emails == ["lee@example.com"]
    assert of_lead.numbers == ["+15550003333"]
