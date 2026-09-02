"""participant and lead email indexes, backfill total

Revision ID: 8998b2d5e588
Revises: 9d4f31e6c8b2
Create Date: 2026-09-01 22:05:22.771124

"""
from alembic import op
import sqlalchemy as sa


revision = '8998b2d5e588'
down_revision = '9d4f31e6c8b2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Address count the current backfill walk is stepping through, stamped
    # when the walk starts so the status poll doesn't rebuild the org's whole
    # contact map to report it. NULL = no walk in progress / not yet started.
    op.add_column(
        "google_accounts",
        sa.Column("gmail_backfill_total", sa.Integer(), nullable=True),
    )
    # Postgres only; SQLite dev falls back to the plain scans, same as the
    # email_messages trigram indexes (pg_trgm is already installed by that
    # migration; the IF NOT EXISTS is belt and braces).
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # A company's mail is every participant at its domain — a LIKE '%@domain'
    # suffix match, which only a trigram index can serve. Without it every
    # company detail view seq-scans a table that grows with every sync.
    op.execute(
        "CREATE INDEX ix_email_participants_email_trgm ON email_participants "
        "USING gin (email gin_trgm_ops)"
    )
    # The public lead forms (and the importer's dedupe) look leads up by
    # lower(email); the plain email index can't answer that.
    op.execute("CREATE INDEX ix_leads_email_lower ON leads (lower(email))")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_leads_email_lower")
        op.execute("DROP INDEX IF EXISTS ix_email_participants_email_trgm")
    op.drop_column("google_accounts", "gmail_backfill_total")
