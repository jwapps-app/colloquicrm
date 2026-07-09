"""contact suggestions

Revision ID: 754773075718
Revises: e26aba7bc773
Create Date: 2026-07-09 12:34:40.016194

"""
from alembic import op
import sqlalchemy as sa


revision = '754773075718'
down_revision = 'e26aba7bc773'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contact_suggestions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "email"),
    )
    op.create_index("ix_contact_suggestions_org_id", "contact_suggestions", ["org_id"])
    op.create_index("ix_contact_suggestions_status", "contact_suggestions", ["status"])


def downgrade() -> None:
    op.drop_index("ix_contact_suggestions_status", table_name="contact_suggestions")
    op.drop_index("ix_contact_suggestions_org_id", table_name="contact_suggestions")
    op.drop_table("contact_suggestions")
