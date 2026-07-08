"""gmail backfill cursor

Revision ID: 0b9de903ae4b
Revises: 90082f0defc3
Create Date: 2026-07-07 22:22:21.593040

"""
from alembic import op
import sqlalchemy as sa


revision = '0b9de903ae4b'
down_revision = '90082f0defc3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "google_accounts",
        sa.Column("gmail_backfill_cursor", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("google_accounts", "gmail_backfill_cursor")
