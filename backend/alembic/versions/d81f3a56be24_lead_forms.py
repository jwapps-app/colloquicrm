"""lead forms

Revision ID: d81f3a56be24
Revises: c7d20a41f9b3
Create Date: 2026-07-11 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'd81f3a56be24'
down_revision = 'c7d20a41f9b3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lead_forms",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("require_email", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("success_message", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("submission_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_lead_forms_org_id", "lead_forms", ["org_id"])
    op.create_index("ix_lead_forms_slug", "lead_forms", ["slug"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_lead_forms_slug", table_name="lead_forms")
    op.drop_index("ix_lead_forms_org_id", table_name="lead_forms")
    op.drop_table("lead_forms")
