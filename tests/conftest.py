"""Shared pytest fixtures.

We run the FastAPI app in-process via httpx's ASGITransport — no live uvicorn,
no sockets, fast and isolated.

Database isolation: tests must never touch the real ./onboarding.db. Each test
gets a fresh in-memory SQLite database (shared across connections within the
test via a StaticPool) that is created, seeded, and torn down per test. We wire
it in by overriding the app's `get_db` dependency.
"""

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.seed import seed
from app.db.session import get_db
from app.main import app


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """A fresh, seeded in-memory database per test."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",  # in-memory
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # one shared connection => in-memory DB persists
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    testing_session = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    async with testing_session() as session:
        await seed(session)
        yield session

    await engine.dispose()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """AsyncClient whose backend uses the isolated test DB via dependency override."""

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.pop(get_db, None)


async def login(client: httpx.AsyncClient, email: str, password: str = "password") -> str:
    """Helper: log in and return the bearer token."""
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
