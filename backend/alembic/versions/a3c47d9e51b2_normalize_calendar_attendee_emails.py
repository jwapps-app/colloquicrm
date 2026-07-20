"""normalize calendar attendee emails

Calendar sync stored attendee emails merely lowercased, while the entity
lookups query them through normalize_email (gmail dots and +suffixes
stripped) — so a dotted-gmail attendee never matched their Person. New rows
are stored normalized; this brings the existing ones in line. The primary
key is (event_id, email), so two variants of one address on the same event
collapse to a single row.

Revision ID: a3c47d9e51b2
Revises: 4b433c982daa
Create Date: 2026-07-20 10:12:41.508273

"""
from alembic import op
import sqlalchemy as sa


revision = 'a3c47d9e51b2'
down_revision = '4b433c982daa'
branch_labels = None
depends_on = None

attendees = sa.table(
    'calendar_event_attendees',
    sa.column('event_id', sa.Uuid()),
    sa.column('email', sa.String()),
)


def _normalize_email(addr: str) -> str:
    # Mirrors app.services.google.normalize_email — copied here so the
    # migration stays frozen even if the app's rule evolves later.
    addr = (addr or '').lower().strip()
    local, _, domain = addr.partition('@')
    if domain in ('gmail.com', 'googlemail.com'):
        local = local.split('+', 1)[0].replace('.', '')
        return f'{local}@gmail.com'
    return addr


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.select(attendees.c.event_id, attendees.c.email)).fetchall()
    seen: set[tuple] = {(eid, email) for eid, email in rows}
    for event_id, email in rows:
        normalized = _normalize_email(email)
        if normalized == email:
            continue
        if (event_id, normalized) in seen:
            # The normalized twin already exists on this event — the variant
            # row is redundant, and updating it would violate the PK.
            bind.execute(
                sa.delete(attendees).where(
                    attendees.c.event_id == event_id, attendees.c.email == email
                )
            )
        else:
            bind.execute(
                sa.update(attendees)
                .where(attendees.c.event_id == event_id, attendees.c.email == email)
                .values(email=normalized)
            )
            seen.add((event_id, normalized))


def downgrade() -> None:
    # Normalization is lossy (the original dotted/suffixed form is gone);
    # nothing to restore, and the next calendar sync rewrites attendees anyway.
    pass
