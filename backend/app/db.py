from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

if not settings.database_url.startswith("postgresql"):
    # The app relies on Postgres behaviour throughout (pg_trgm search, row
    # locks, RETURNING, enforced foreign keys) and its schema exists only as
    # Alembic migrations. Say so at startup rather than failing oddly later.
    raise RuntimeError(
        "DATABASE_URL must be a PostgreSQL URL (postgresql+asyncpg://user:pass@host:5432/db)"
    )

# The background loops (reminders, Google, RingCentral, purge, attachments,
# automations) each hold a connection while they run, on top of request
# traffic — the default 5+10 pool had requests queueing behind syncs. Twenty
# in total stays well inside the sidecar Postgres's connection budget.
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=10,
    max_overflow=10,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
