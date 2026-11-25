import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Text, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app import models  # noqa: F401  (registers all tables on Base.metadata)
from app.config import settings
from app.crypto import EncryptedString
from app.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Config values go through configparser interpolation, where a bare % is a
# syntax error — and a correctly URL-encoded password is full of them
# (p%40ss). Doubling is configparser's own escape; it reads back as one %.
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def compare_type(ctx, inspected_column, metadata_column, inspected_type, metadata_type):
    """`alembic check` compares each column's type with the model's. An
    EncryptedString declared without a length is stored as TEXT (see
    app/crypto.py) but renders as an unsized VARCHAR under asyncpg, which
    would read as drift on every run. Say what it really is; everything else
    gets the default comparison (None)."""
    if isinstance(metadata_type, EncryptedString) and metadata_type.impl.length is None:
        return not isinstance(inspected_type, Text)
    return None


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, compare_type=compare_type
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
