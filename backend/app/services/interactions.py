"""Copper-style relationship metrics: a person's interaction count and
last-contacted date, computed from every engaged touchpoint we sync —
direct emails, calls, and texts."""

import uuid

from sqlalchemy import func, select

from app.models import EmailMessage, EmailParticipant, Person, PhoneEvent


async def update_person_aggregates(db, org_id: uuid.UUID, person_ids: set[uuid.UUID]) -> None:
    from app.services.google import normalize_email
    from app.services.ringcentral import normalize_phone

    await db.flush()  # pending rows must be visible (autoflush is off)
    for pid in person_ids:
        person = (
            await db.execute(select(Person).where(Person.id == pid, Person.org_id == org_id))
        ).scalar_one_or_none()
        if person is None:
            continue
        emails = [normalize_email(e) for e in (person.work_email, person.personal_email) if e]
        numbers = [
            n for n in (normalize_phone(person.work_phone), normalize_phone(person.mobile_phone)) if n
        ]

        email_count, email_latest = 0, None
        if emails:
            email_count, email_latest = (
                await db.execute(
                    select(
                        func.count(func.distinct(EmailMessage.id)), func.max(EmailMessage.sent_at)
                    )
                    .join(EmailParticipant, EmailParticipant.email_id == EmailMessage.id)
                    .where(
                        EmailMessage.org_id == org_id,
                        EmailParticipant.email.in_(emails),
                        EmailParticipant.direct.is_(True),
                    )
                )
            ).one()

        from sqlalchemy import or_

        phone_conditions = [
            (PhoneEvent.entity_type == "person") & (PhoneEvent.entity_id == pid)
        ]
        if numbers:
            phone_conditions.append(PhoneEvent.other_number.in_(numbers))
        phone_count, phone_latest = (
            await db.execute(
                select(func.count(func.distinct(PhoneEvent.id)), func.max(PhoneEvent.happened_at)).where(
                    PhoneEvent.org_id == org_id, or_(*phone_conditions)
                )
            )
        ).one()

        person.interaction_count = (email_count or 0) + (phone_count or 0)
        latest = max((d for d in (email_latest, phone_latest) if d is not None), default=None)
        if latest is not None:
            person.last_contacted_at = latest
