"""soft delete deleted_at

Revision ID: 9e6619ffb28f
Revises: 754773075718
Create Date: 2026-07-09 13:01:29.924478

"""
from alembic import op
import sqlalchemy as sa


revision = '9e6619ffb28f'
down_revision = '754773075718'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_companies_deleted_at", "companies", ["deleted_at"])
    op.add_column("people", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_people_deleted_at", "people", ["deleted_at"])
    op.add_column("leads", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_leads_deleted_at", "leads", ["deleted_at"])
    op.add_column("opportunities", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_opportunities_deleted_at", "opportunities", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_opportunities_deleted_at", table_name="opportunities")
    op.drop_column("opportunities", "deleted_at")
    op.drop_index("ix_leads_deleted_at", table_name="leads")
    op.drop_column("leads", "deleted_at")
    op.drop_index("ix_people_deleted_at", table_name="people")
    op.drop_column("people", "deleted_at")
    op.drop_index("ix_companies_deleted_at", table_name="companies")
    op.drop_column("companies", "deleted_at")
