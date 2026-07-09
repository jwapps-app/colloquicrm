"""trigram indexes for email search

Revision ID: 7883a28cb8fe
Revises: 9e6619ffb28f
Create Date: 2026-07-09 13:53:05.852944

"""
from alembic import op
import sqlalchemy as sa


revision = '7883a28cb8fe'
down_revision = '9e6619ffb28f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # pg_trgm GIN indexes let the leading-wildcard ILIKE email search use an
    # index instead of seq-scanning (and lower()-ing) every archived body on
    # each query. Postgres only; SQLite dev falls back to the plain scan.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # The search ORs ILIKE over these five columns; the planner can only
    # BitmapOr the branches if every one is indexable.
    for col in ("subject", "snippet", "from_name", "from_email", "body_text"):
        op.execute(
            f"CREATE INDEX ix_email_messages_{col}_trgm ON email_messages "
            f"USING gin ({col} gin_trgm_ops)"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for col in ("body_text", "from_email", "from_name", "snippet", "subject"):
        op.execute(f"DROP INDEX IF EXISTS ix_email_messages_{col}_trgm")
