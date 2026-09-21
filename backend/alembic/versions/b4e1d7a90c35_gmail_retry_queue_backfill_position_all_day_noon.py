"""gmail retry queue, backfill position, all-day events at noon

Revision ID: b4e1d7a90c35
Revises: 8998b2d5e588
Create Date: 2026-09-21 10:12:44.118392

"""
from alembic import op
import sqlalchemy as sa


revision = 'b4e1d7a90c35'
down_revision = '8998b2d5e588'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Where the backfill walk really is: the last address searched to
    # completion, plus the page position inside the current group's search.
    # Both NULL on existing rows — an unfinished walk simply starts over from
    # the first address, and the gmail_id dedupe makes the repeat cheap.
    op.add_column(
        "google_accounts", sa.Column("gmail_backfill_after", sa.String(320), nullable=True)
    )
    op.add_column(
        "google_accounts",
        sa.Column("gmail_backfill_page_token", sa.String(1000), nullable=True),
    )
    # Messages that would not fetch and addresses whose search errored, owed
    # a retry. Cascades with the account, so unlinking clears the queue.
    op.create_table(
        "gmail_sync_failures",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("key", sa.String(320), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("given_up", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["google_accounts.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "kind", "key"),
    )

    # All-day events were stored at 00:00 UTC of their date, which a client
    # west of UTC renders as the day before. They now live at 12:00 UTC, so
    # every zone from UTC-11 to UTC+11 lands on the right calendar date.
    # Only rows sitting at exactly midnight UTC move, so running this twice
    # (or over rows the new sync already wrote) shifts nothing a second time.
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        for col in ("starts_at", "ends_at"):
            op.execute(
                f"UPDATE calendar_events SET {col} = {col} + interval '12 hours' "
                f"WHERE all_day AND {col} IS NOT NULL "
                f"AND ({col} AT TIME ZONE 'UTC')::time = time '00:00:00'"
            )
    elif dialect == "sqlite":
        # Dev only. Timestamps are text there ("YYYY-MM-DD HH:MM:SS.ffffff").
        for col in ("starts_at", "ends_at"):
            op.execute(
                f"UPDATE calendar_events SET {col} = replace({col}, ' 00:00:00', ' 12:00:00') "
                f"WHERE all_day AND {col} LIKE '% 00:00:00%'"
            )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        for col in ("starts_at", "ends_at"):
            op.execute(
                f"UPDATE calendar_events SET {col} = {col} - interval '12 hours' "
                f"WHERE all_day AND {col} IS NOT NULL "
                f"AND ({col} AT TIME ZONE 'UTC')::time = time '12:00:00'"
            )
    elif dialect == "sqlite":
        for col in ("starts_at", "ends_at"):
            op.execute(
                f"UPDATE calendar_events SET {col} = replace({col}, ' 12:00:00', ' 00:00:00') "
                f"WHERE all_day AND {col} LIKE '% 12:00:00%'"
            )
    op.drop_table("gmail_sync_failures")
    op.drop_column("google_accounts", "gmail_backfill_page_token")
    op.drop_column("google_accounts", "gmail_backfill_after")
