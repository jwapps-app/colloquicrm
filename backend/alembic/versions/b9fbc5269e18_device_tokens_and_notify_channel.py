"""device tokens and notify channel

Revision ID: b9fbc5269e18
Revises: 7883a28cb8fe
Create Date: 2026-07-09 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'b9fbc5269e18'
down_revision = '7883a28cb8fe'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token", sa.String(200), nullable=False),
        sa.Column("platform", sa.String(20), nullable=False, server_default="ios"),
        sa.Column("environment", sa.String(20), nullable=False, server_default="production"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_device_tokens_org_id", "device_tokens", ["org_id"])
    op.create_index("ix_device_tokens_user_id", "device_tokens", ["user_id"])
    op.create_index("ix_device_tokens_token", "device_tokens", ["token"], unique=True)
    op.add_column(
        "users",
        sa.Column(
            "notify_channel", sa.String(20), nullable=False, server_default="colloqui_chat"
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "notify_channel")
    op.drop_index("ix_device_tokens_token", table_name="device_tokens")
    op.drop_index("ix_device_tokens_user_id", table_name="device_tokens")
    op.drop_index("ix_device_tokens_org_id", table_name="device_tokens")
    op.drop_table("device_tokens")
