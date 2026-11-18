"""Candidate route tests against the database-backed implementation.

Verifies each candidate sees their OWN profile (the old mock returned a fixed
Alex Rivera profile for everyone) and that the offer letter reflects real data.
"""

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.models import Candidate, InterviewStage, User
from tests.conftest import auth_header, login


async def _cand(client: httpx.AsyncClient, email: str = "candidate@example.com") -> dict[str, str]:
    return auth_header(await login(client, email))


async def test_candidate_me_returns_own_profile(client: httpx.AsyncClient) -> None:
    h = await _cand(client)
    resp = await client.get("/api/v1/candidate/me", headers=h)
    assert resp.status_code == 200
    data = resp.json()
    assert data["candidate_id"] == "cand-1"
    assert data["full_name"] == "Alex Rivera"
    assert data["email"] == "candidate@example.com"
    assert len(data["interview_stages"]) == 5
    # cand-1's in_progress stage is s3 at index 2.
    assert data["current_stage_index"] == 2
    assert data["offer"]["available"] is True
    assert data["offer"]["job_title"] == "Senior Software Engineer"


async def test_offer_letter_download_is_pdf(client: httpx.AsyncClient) -> None:
    h = await _cand(client)
    resp = await client.get("/api/v1/candidate/offer-letter", headers=h)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].endswith('.pdf"')
    # A valid PDF starts with the %PDF magic bytes.
    assert resp.content[:4] == b"%PDF"
    assert len(resp.content) > 500  # a real document, not an empty stub


async def test_each_candidate_sees_only_their_own_data(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    # Create a second candidate user with a distinct profile.
    db_session.add(
        User(
            id="cand-9",
            email="second@example.com",
            hashed_password=hash_password("password"),
            display_name="Dana Second",
            role="candidate",
        )
    )
    cand = Candidate(
        id="cand-9",
        user_id="cand-9",
        full_name="Dana Second",
        email="second@example.com",
        job_title="Data Analyst",
        salary_display="USD 110,000",
        pipeline_status="interviewing",
        onboarding_status="in_progress",
        offer_available=False,
    )
    cand.interview_stages = [
        InterviewStage(id="z1", name="Recruiter screen", status="in_progress", position=0),
    ]
    db_session.add(cand)
    await db_session.commit()

    h = await _cand(client, "second@example.com")
    resp = await client.get("/api/v1/candidate/me", headers=h)
    assert resp.status_code == 200
    data = resp.json()
    # Must be Dana's data, NOT Alex's — proving the old fixed-profile bug is gone.
    assert data["candidate_id"] == "cand-9"
    assert data["full_name"] == "Dana Second"
    assert data["offer"]["available"] is False


async def test_candidate_without_profile_gets_404(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    # A candidate-role user with no linked Candidate row.
    db_session.add(
        User(
            id="cand-orphan",
            email="orphan@example.com",
            hashed_password=hash_password("password"),
            display_name="No Profile",
            role="candidate",
        )
    )
    await db_session.commit()

    h = await _cand(client, "orphan@example.com")
    resp = await client.get("/api/v1/candidate/me", headers=h)
    assert resp.status_code == 404


async def test_offer_letter_404_when_no_offer(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    # A candidate whose offer is not yet available cannot download a letter.
    db_session.add(
        User(
            id="cand-nooffer",
            email="nooffer@example.com",
            hashed_password=hash_password("password"),
            display_name="Pat Pending",
            role="candidate",
        )
    )
    cand = Candidate(
        id="cand-nooffer",
        user_id="cand-nooffer",
        full_name="Pat Pending",
        email="nooffer@example.com",
        job_title="Analyst",
        salary_display="TBD",
        pipeline_status="interviewing",
        onboarding_status="in_progress",
        offer_available=False,
    )
    db_session.add(cand)
    await db_session.commit()

    h = await _cand(client, "nooffer@example.com")
    resp = await client.get("/api/v1/candidate/offer-letter", headers=h)
    assert resp.status_code == 404
