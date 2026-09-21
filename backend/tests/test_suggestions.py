"""Contact suggestions: accepting one creates a person who already has the
mail that got them suggested."""

import uuid
from datetime import timedelta

from app.models import ContactSuggestion, EmailMessage, EmailParticipant, Person, utcnow

from .conftest import add


async def test_accepted_suggestion_starts_with_the_mail_that_suggested_it(
    client, world, db_session
):
    sent_at = utcnow() - timedelta(days=2)
    for when in (sent_at, sent_at - timedelta(days=9)):
        msg = await add(
            EmailMessage(
                org_id=world.org.id,
                gmail_id=uuid.uuid4().hex[:16],
                rfc_message_id=f"<{uuid.uuid4().hex}@mail.example>",
                from_email="frequent@partner.example",
                sent_at=when,
            )
        )
        await add(
            EmailParticipant(
                email_id=msg.id, email="frequent@partner.example", kind="from", direct=True
            )
        )
    suggestion = await add(
        ContactSuggestion(
            org_id=world.org.id, email="frequent@partner.example", message_count=2
        )
    )

    resp = await client.post(
        f"/api/v1/contact-suggestions/{suggestion.id}/add", headers=world.member_auth
    )
    assert resp.status_code == 200, resp.text
    async with db_session() as db:
        person = await db.get(Person, uuid.UUID(resp.json()["person_id"]))
    assert (person.interaction_count, person.last_contacted_at) == (2, sent_at)
