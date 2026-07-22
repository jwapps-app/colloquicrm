"""file attachments

Revision ID: e5b82c41f7a9
Revises: a3c47d9e51b2
Create Date: 2026-07-22 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'e5b82c41f7a9'
down_revision = 'a3c47d9e51b2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(30), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("stored_name", sa.String(80), nullable=False, unique=True),
        sa.Column(
            "uploaded_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_attachments_org_id", "attachments", ["org_id"])
    op.create_index(
        "ix_attachments_entity", "attachments", ["org_id", "entity_type", "entity_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_attachments_entity", table_name="attachments")
    op.drop_index("ix_attachments_org_id", table_name="attachments")
    op.drop_table("attachments")
