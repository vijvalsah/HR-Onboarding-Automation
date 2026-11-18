"""Async engine, session factory, FastAPI dependency, and schema migration.

One engine is created per process. `get_db` yields a short-lived session per
request and always closes it. `init_db` runs Alembic migrations (upgrade head)
so the schema is current before the app serves traffic.
"""

from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

# Repo root = two levels up from this file (app/db/session.py -> repo).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# `future=True` is implied in SQLAlchemy 2.0. echo follows DEBUG for dev insight.
engine = create_async_engine(
    settings.database_url,
    echo=settings.db_echo,
    pool_pre_ping=True,
)

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yields a request-scoped session."""
    async with SessionLocal() as session:
        yield session


def _run_alembic_upgrade() -> None:
    """Bring the database up to the latest migration (alembic upgrade head).

    Alembic now owns the schema, so we run migrations instead of create_all.
    A fresh database is built from the migration scripts; an existing one gets
    any new migrations applied. This is synchronous (Alembic manages its own
    connection), so callers invoke it via a thread from async startup.
    """
    from alembic import command
    from alembic.config import Config

    from app.core.config import settings

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")


async def init_db() -> None:
    """Apply migrations so the schema is current before the app serves traffic."""
    import asyncio

    # Importing models keeps Base.metadata populated for autogenerate tooling.
    from app.db import models  # noqa: F401

    await asyncio.to_thread(_run_alembic_upgrade)
