"""attention and note feed indexes

Revision ID: 4b433c982daa
Revises: f3a91c2b7e04
Create Date: 2026-07-16 11:04:12.138123

"""
from alembic import op
import sqlalchemy as sa


revision = '4b433c982daa'
down_revision = 'f3a91c2b7e04'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive index-only migration. sqlite dev builds its schema from the
    # models via create_all, so these already exist there — only real
    # databases need the DDL.
    if op.get_bind().dialect.name == "sqlite":
        return
    # Backs the badge/attention count (run on every push), the reminder sweep,
    # the assignee list filter, and the activity report group-by.
    op.create_index(
        "ix_tasks_assignee_status_due", "tasks", ["assignee_id", "status", "due_at"]
    )
    # The feed orders notes by created_at; the activity report filters and
    # groups notes by org and date.
    op.create_index("ix_notes_org_created", "notes", ["org_id", "created_at"])


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    op.drop_index("ix_notes_org_created", table_name="notes")
    op.drop_index("ix_tasks_assignee_status_due", table_name="tasks")
