"""people.last_contacted_imported_at

Revision ID: d7f2b6a3c918
Revises: c4e8a1f07b35
Create Date: 2026-09-21 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'd7f2b6a3c918'
down_revision = 'c4e8a1f07b35'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # An imported "Last Contacted" date shared a column with the date the
    # email/call sync derives, so the first sync overwrote it for good. It
    # gets a column of its own; last_contacted_at becomes the later of the two.
    op.add_column(
        "people",
        sa.Column("last_contacted_imported_at", sa.DateTime(timezone=True), nullable=True),
    )
    # A date on a person with no counted interactions can only have come from
    # an import (or a manual edit) — exactly the dates still intact. Anyone
    # with interactions already carries a sync-derived date; nothing to save.
    # Plain column-to-column copy: the same statement on Postgres and SQLite,
    # so no dialect branch. Raw Core update — updated_at is not touched.
    people = sa.table(
        "people",
        sa.column("last_contacted_at", sa.DateTime(timezone=True)),
        sa.column("last_contacted_imported_at", sa.DateTime(timezone=True)),
        sa.column("interaction_count", sa.Integer),
    )
    op.execute(
        people.update()
        .where(people.c.interaction_count == 0, people.c.last_contacted_at.is_not(None))
        .values(last_contacted_imported_at=people.c.last_contacted_at)
    )


def downgrade() -> None:
    # last_contacted_at already holds the later of the two dates; dropping
    # the column loses only the ability to recover an older imported one.
    op.drop_column("people", "last_contacted_imported_at")
