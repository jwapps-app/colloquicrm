"""canonical lead statuses and checkbox values, opportunity closed_at

Revision ID: c4e8a1f07b35
Revises: b4e1d7a90c35
Create Date: 2026-09-21 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c4e8a1f07b35'
down_revision = 'b4e1d7a90c35'
branch_labels = None
depends_on = None

# Frozen copies of the application's canonical sets — a migration must keep
# meaning what it meant the day it ran, whatever the app constants become.
LEAD_STATUSES = ["New", "Open", "Contacted", "Qualified", "Unqualified", "Converted"]
CHECKBOX_TRUE = ["true", "1", "yes", "y", "on", "t", "checked"]
CHECKBOX_FALSE = ["false", "0", "no", "n", "off", "f", "unchecked", ""]


def upgrade() -> None:
    # When a deal left "open". Sales reports used close_date, else updated_at
    # — and updated_at moves every time someone edits the record.
    op.add_column(
        "opportunities",
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill already-closed deals with the date the old report would have
    # used, so history doesn't shift: close_date when set, else updated_at.
    # A close_date becomes midnight UTC of that day. SQLite (dev) stores
    # datetimes as text, so the same value is spelled out by hand there.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "UPDATE opportunities SET closed_at = COALESCE("
            "(close_date::timestamp AT TIME ZONE 'UTC'), updated_at, created_at) "
            "WHERE status IN ('won', 'lost', 'abandoned') AND closed_at IS NULL"
        )
    else:
        op.execute(
            "UPDATE opportunities SET closed_at = COALESCE("
            "close_date || ' 00:00:00.000000', updated_at, created_at) "
            "WHERE status IN ('won', 'lost', 'abandoned') AND closed_at IS NULL"
        )

    # Lead statuses: one casing. The model default and the public forms wrote
    # "New" while the web app wrote "new", and the list filter was exact.
    leads = sa.table("leads", sa.column("status", sa.String))
    for status in LEAD_STATUSES:
        op.execute(
            leads.update()
            .where(
                sa.func.lower(sa.func.trim(leads.c.status)) == status.lower(),
                leads.c.status != status,
            )
            .values(status=status)
        )

    # Checkbox custom values: lowercase "true"/"false". Python's str(False)
    # used to land here as "False", which every client read as checked.
    # Values outside both lists are left alone rather than guessed at.
    fields = sa.table(
        "custom_fields", sa.column("id", sa.Uuid), sa.column("field_type", sa.String)
    )
    values = sa.table(
        "custom_field_values", sa.column("field_id", sa.Uuid), sa.column("value", sa.Text)
    )
    checkbox_fields = sa.select(fields.c.id).where(fields.c.field_type == "checkbox")
    normalized = sa.func.lower(sa.func.trim(values.c.value))
    for canonical, variants in (("true", CHECKBOX_TRUE), ("false", CHECKBOX_FALSE)):
        op.execute(
            values.update()
            .where(
                values.c.field_id.in_(checkbox_fields),
                values.c.value.is_not(None),
                normalized.in_(variants),
                values.c.value != canonical,
            )
            .values(value=canonical)
        )


def downgrade() -> None:
    # The casing/boolean rewrites are not reversible (and nothing needs the
    # old spellings back); only the column goes.
    op.drop_column("opportunities", "closed_at")
