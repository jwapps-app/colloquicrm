"""encrypt sensitive secret columns at rest

Widens the affected columns to fit Fernet ciphertext and encrypts every
existing plaintext value in place. The data step is idempotent: values that
already decrypt as ciphertext are skipped, so re-running is safe.

Revision ID: f3a91c2b7e04
Revises: d81f3a56be24
Create Date: 2026-07-12

"""
import sqlalchemy as sa
from alembic import op

from app.crypto import encrypt, is_encrypted

revision = "f3a91c2b7e04"
down_revision = "d81f3a56be24"
branch_labels = None
depends_on = None


# (table, column, old varchar length, new varchar length). TEXT columns are not
# listed here — they already fit ciphertext and need no widening.
_WIDEN = [
    ("users", "totp_secret", 64, 255),
    ("colloqui_integration", "api_key", 255, 512),
    ("google_integration", "client_secret", 255, 512),
    ("google_accounts", "refresh_token", 512, 1024),
    ("google_accounts", "access_token", 2048, 4096),
    ("ringcentral_integration", "client_secret", 255, 512),
]

# Every (table, column) holding a secret that must be encrypted, including the
# TEXT columns that weren't widened.
_ENCRYPT = [
    ("users", "totp_secret", "id"),
    ("colloqui_integration", "api_key", "org_id"),
    ("google_integration", "client_secret", "org_id"),
    ("google_accounts", "refresh_token", "user_id"),
    ("google_accounts", "access_token", "user_id"),
    ("ringcentral_integration", "client_secret", "org_id"),
    ("ringcentral_integration", "jwt", "org_id"),
    ("ringcentral_integration", "access_token", "org_id"),
]


def _encrypt_existing(bind) -> None:
    """Encrypt every non-null, not-already-encrypted secret value in place."""
    for table, column, pk in _ENCRYPT:
        rows = bind.execute(
            sa.text(f"SELECT {pk} AS pk, {column} AS val FROM {table} WHERE {column} IS NOT NULL")
        ).fetchall()
        for row in rows:
            val = row.val
            if val is None or is_encrypted(val):
                continue  # idempotent: skip already-ciphertext rows
            bind.execute(
                sa.text(f"UPDATE {table} SET {column} = :val WHERE {pk} = :pk"),
                {"val": encrypt(val), "pk": row.pk},
            )


def upgrade() -> None:
    bind = op.get_bind()
    # sqlite can't ALTER column types in place; dev uses create_all so the
    # widths are already correct there. Only widen on real databases.
    if bind.dialect.name != "sqlite":
        for table, column, old_len, new_len in _WIDEN:
            op.alter_column(
                table,
                column,
                existing_type=sa.String(length=old_len),
                type_=sa.String(length=new_len),
                existing_nullable=None,
            )
    _encrypt_existing(bind)


def downgrade() -> None:
    # Decrypt values back to plaintext so no data is lost, then (on non-sqlite)
    # narrow the columns back to their original widths.
    bind = op.get_bind()
    from app.crypto import decrypt

    for table, column, pk in _ENCRYPT:
        rows = bind.execute(
            sa.text(f"SELECT {pk} AS pk, {column} AS val FROM {table} WHERE {column} IS NOT NULL")
        ).fetchall()
        for row in rows:
            if row.val is None or not is_encrypted(row.val):
                continue
            bind.execute(
                sa.text(f"UPDATE {table} SET {column} = :val WHERE {pk} = :pk"),
                {"val": decrypt(row.val), "pk": row.pk},
            )
    if bind.dialect.name != "sqlite":
        for table, column, old_len, new_len in _WIDEN:
            op.alter_column(
                table,
                column,
                existing_type=sa.String(length=new_len),
                type_=sa.String(length=old_len),
                existing_nullable=None,
            )
