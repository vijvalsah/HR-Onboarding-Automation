"""Recruiting pipeline tests: the full ATS flow through the API."""

import httpx
import pytest

from app.services.integrations import email_service
from tests.conftest import auth_header, login


async def _hr(client: httpx.AsyncClient) -> dict[str, str]:
    return auth_header(await login(client, "hr@example.com"))


async def test_list_seeded_jobs(client: httpx.AsyncClient) -> None:
    h = await _hr(client)
    resp = await client.get("/api/v1/recruiting/jobs", headers=h)
    assert resp.status_code == 200
    jobs = resp.json()
    assert any(j["title"] == "Senior Software Engineer" for j in jobs)


async def test_candidate_role_denied_recruiting(client: httpx.AsyncClient) -> None:
    token = await login(client, "candidate@example.com")
    resp = await client.get("/api/v1/recruiting/jobs", headers=auth_header(token))
    assert resp.status_code == 403


async def test_screen_strong_applicant_shortlists(client: httpx.AsyncClient) -> None:
    h = await _hr(client)
    resp = await client.post("/api/v1/recruiting/applications/app-1/screen", headers=h)
    assert resp.status_code == 200
    data = resp.json()
    assert data["screening_score"] >= 60
    assert data["shortlisted"] is True
    assert data["status"] == "shortlisted"


async def test_screen_weak_applicant_not_shortlisted(client: httpx.AsyncClient) -> None:
    h = await _hr(client)
    resp = await client.post("/api/v1/recruiting/applications/app-2/screen", headers=h)
    assert resp.status_code == 200
    data = resp.json()
    assert data["shortlisted"] is False


async def test_call_bot_requires_shortlist_then_confirms(client: httpx.AsyncClient) -> None:
    h = await _hr(client)
    # Before shortlisting, call-bot is rejected.
    early = await client.post("/api/v1/recruiting/applications/app-1/call-bot", headers=h)
    assert early.status_code == 409
    # Shortlist, then the call-bot confirms availability.
    await client.post("/api/v1/recruiting/applications/app-1/screen", headers=h)
    resp = await client.post("/api/v1/recruiting/applications/app-1/call-bot", headers=h)
    assert resp.status_code == 200
    assert resp.json()["availability_confirmed"] is True


async def test_assign_interviewer_sends_email_and_teams_link(
    client: httpx.AsyncClient,
) -> None:
    h = await _hr(client)
    await client.post("/api/v1/recruiting/applications/app-1/screen", headers=h)
    before = len(email_service.outbox)  # type: ignore[attr-defined]

    resp = await client.post(
        "/api/v1/recruiting/applications/app-1/assign-interviewer",
        headers=h,
        json={"interviewer_user_id": "int-1", "round_name": "Technical"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["meeting_url"].startswith("https://teams.microsoft.com/")
    assert data["interviewer_user_id"] == "int-1"
    # An email was sent to the candidate with the join link.
    assert len(email_service.outbox) == before + 1  # type: ignore[attr-defined]
    assert email_service.outbox[-1].to == "morgan.patel@example.com"  # type: ignore[attr-defined]


async def test_assign_rejects_non_interviewer(client: httpx.AsyncClient) -> None:
    h = await _hr(client)
    await client.post("/api/v1/recruiting/applications/app-1/screen", headers=h)
    resp = await client.post(
        "/api/v1/recruiting/applications/app-1/assign-interviewer",
        headers=h,
        json={"interviewer_user_id": "hr-1"},  # hr, not interviewer
    )
    assert resp.status_code == 400


async def test_reject_application_one_click(client: httpx.AsyncClient) -> None:
    h = await _hr(client)
    resp = await client.post("/api/v1/recruiting/applications/app-2/reject", headers=h)
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


async def test_full_pipeline_to_offer(client: httpx.AsyncClient) -> None:
    """Walk a single-round job all the way to an offer."""
    h = await _hr(client)
    # A job with exactly 1 round to keep the walk short.
    job = await client.post(
        "/api/v1/recruiting/jobs",
        headers=h,
        json={
            "title": "Backend Engineer",
            "requirements": "python, fastapi, sqlalchemy",
            "num_rounds": 1,
            "shortlist_threshold": 50,
            "salary_display": "USD 130,000",
        },
    )
    job_id = job.json()["id"]

    app_resp = await client.post(
        "/api/v1/recruiting/applications",
        json={
            "job_id": job_id,
            "applicant_name": "Sam Strong",
            "applicant_email": "sam.strong@example.com",
            "resume_text": "Expert in python, fastapi and sqlalchemy backend systems.",
        },
    )
    app_id = app_resp.json()["id"]

    # Screen -> shortlist.
    screen = await client.post(f"/api/v1/recruiting/applications/{app_id}/screen", headers=h)
    assert screen.json()["shortlisted"] is True
    # Call-bot.
    await client.post(f"/api/v1/recruiting/applications/{app_id}/call-bot", headers=h)
    # Assign interviewer -> creates round 1.
    assign = await client.post(
        f"/api/v1/recruiting/applications/{app_id}/assign-interviewer",
        headers=h,
        json={"interviewer_user_id": "int-1", "round_name": "Final"},
    )
    round_id = assign.json()["round_id"]
    # Advance (pass) the final round -> offer generated.
    decision = await client.post(
        f"/api/v1/recruiting/rounds/{round_id}/decision",
        headers=h,
        json={"decision": "advance"},
    )
    assert decision.status_code == 200
    data = decision.json()
    assert data["application_status"] == "offer"
    assert data["offer_generated"] is True
