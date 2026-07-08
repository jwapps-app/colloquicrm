"""entity tags entity index

Revision ID: 10bd28374990
Revises: 6f7e9260f4b3
Create Date: 2026-07-07 22:12:12.357486

"""
from alembic import op
import sqlalchemy as sa


revision = '10bd28374990'
down_revision = '6f7e9260f4b3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_entity_tags_entity", "entity_tags", ["entity_type", "entity_id"])
    op.create_index(
        "ix_email_messages_owner_gmail", "email_messages", ["owner_user_id", "gmail_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_email_messages_owner_gmail", table_name="email_messages")
    op.drop_index("ix_entity_tags_entity", table_name="entity_tags")
