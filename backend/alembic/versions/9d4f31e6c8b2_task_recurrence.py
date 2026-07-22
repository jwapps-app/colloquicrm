"""task recurrence

Revision ID: 9d4f31e6c8b2
Revises: e5b82c41f7a9
Create Date: 2026-07-22 09:05:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '9d4f31e6c8b2'
down_revision = 'e5b82c41f7a9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("repeat_every", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("repeat_unit", sa.String(10), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "repeat_unit")
    op.drop_column("tasks", "repeat_every")
