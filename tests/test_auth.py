"""Auth-specific tests: prove passwords are hashed (not plaintext) and that
login validates against the database, not a hardcoded dict.
"""

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, verify_password
from app.db.models import User


async def test_stored_passwords_are_bcrypt_hashes(db_session: AsyncSession) -> None:
    result = await db_session.execute(select(User))
    users = result.scalars().all()
    assert users, "seed should create users"
    for u in users:
        # bcrypt hashes start with $2b$ and are never equal to the plaintext.
        assert u.hashed_password.startswith("$2b$")
        assert u.hashed_password != "password"


async def test_login_uses_database_not_dict(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    # Add a brand-new user directly to the DB with a distinct password.
    db_session.add(
        User(
            id="qa-1",
            email="qa@example.com",
            hashed_password=hash_password("s3cret-qa"),
            display_name="QA Tester",
            role="hr",
        )
    )
    await db_session.commit()

    # Correct password logs in.
    ok = await client.post(
        "/api/v1/auth/login",
        json={"email": "qa@example.com", "password": "s3cret-qa"},
    )
    assert ok.status_code == 200
    assert ok.json()["access_token"]

    # Wrong password is rejected.
    bad = await client.post(
        "/api/v1/auth/login",
        json={"email": "qa@example.com", "password": "wrong"},
    )
    assert bad.status_code == 401


def test_hash_roundtrip() -> None:
    h = hash_password("hunter2")
    assert verify_password("hunter2", h)
    assert not verify_password("Hunter2", h)
    assert not verify_password("", h)
