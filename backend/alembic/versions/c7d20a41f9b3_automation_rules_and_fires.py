"""automation rules and fires

Revision ID: c7d20a41f9b3
Revises: b9fbc5269e18
Create Date: 2026-07-11 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c7d20a41f9b3'
down_revision = 'b9fbc5269e18'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "automation_rules",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("entity_type", sa.String(30), nullable=False),
        sa.Column("trigger_type", sa.String(30), nullable=False),
        sa.Column("trigger_config", sa.JSON(), nullable=False),
        sa.Column("action_type", sa.String(30), nullable=False),
        sa.Column("action_config", sa.JSON(), nullable=False),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_automation_rules_org_id", "automation_rules", ["org_id"])

    op.create_table(
        "automation_fires",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "org_id",
            sa.Uuid(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "rule_id",
            sa.Uuid(),
            sa.ForeignKey("automation_rules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(30), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.UniqueConstraint("rule_id", "entity_type", "entity_id"),
    )
    op.create_index("ix_automation_fires_org_id", "automation_fires", ["org_id"])
    op.create_index("ix_automation_fires_rule_id", "automation_fires", ["rule_id"])


def downgrade() -> None:
    op.drop_index("ix_automation_fires_rule_id", table_name="automation_fires")
    op.drop_index("ix_automation_fires_org_id", table_name="automation_fires")
    op.drop_table("automation_fires")
    op.drop_index("ix_automation_rules_org_id", table_name="automation_rules")
    op.drop_table("automation_rules")
