"""totp attempt counter

Revision ID: e26aba7bc773
Revises: 0b9de903ae4b
Create Date: 2026-07-07 22:24:37.700706

"""
from alembic import op
import sqlalchemy as sa


revision = 'e26aba7bc773'
down_revision = '0b9de903ae4b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("totp_attempts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("sessions", "totp_attempts")
