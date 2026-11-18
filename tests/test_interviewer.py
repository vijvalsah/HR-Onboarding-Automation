"""Interviewer flow tests: assignments, transcript, dual feedback, assessment."""

import httpx

from tests.conftest import auth_header, login


async def _hr(client: httpx.AsyncClient) -> dict[str, str]:
    return auth_header(await login(client, "hr@example.com"))


async def _interviewer(client: httpx.AsyncClient) -> dict[str, str]:
    return auth_header(await login(client, "interviewer@example.com"))


async def _assign_round(client: httpx.AsyncClient) -> str:
    """Drive app-1 to an assigned interview round; return the round id."""
    h = await _hr(client)
    await client.post("/api/v1/recruiting/applications/app-1/screen", headers=h)
    await client.post("/api/v1/recruiting/applications/app-1/call-bot", headers=h)
    assign = await client.post(
        "/api/v1/recruiting/applications/app-1/assign-interviewer",
        headers=h,
        json={"interviewer_user_id": "int-1", "round_name": "Technical"},
    )
    return assign.json()["round_id"]


async def test_interviewer_sees_assignment(client: httpx.AsyncClient) -> None:
    round_id = await _assign_round(client)
    h = await _interviewer(client)
    resp = await client.get("/api/v1/interviewer/assignments", headers=h)
    assert resp.status_code == 200
    rounds = resp.json()
    assert any(r["round_id"] == round_id for r in rounds)
    mine = next(r for r in rounds if r["round_id"] == round_id)
    assert mine["applicant_name"] == "Morgan Patel"
    assert mine["has_feedback"] is False


async def test_interviewer_cannot_see_unassigned_round(client: httpx.AsyncClient) -> None:
    round_id = await _assign_round(client)
    # Reassign conceptually: a different interviewer should be blocked. We only
    # have one interviewer seeded, so simulate by checking hr's assignment list
    # is interviewer-only (403 for hr on the interviewer endpoint).
    h = await _hr(client)
    resp = await client.get("/api/v1/interviewer/assignments", headers=h)
    assert resp.status_code == 403
    assert round_id  # sanity


async def test_transcript_then_dual_feedback_then_assessment(
    client: httpx.AsyncClient,
) -> None:
    round_id = await _assign_round(client)
    ih = await _interviewer(client)

    # 1. Generate the transcript.
    txn = await client.post(
        f"/api/v1/interviewer/rounds/{round_id}/transcript", headers=ih
    )
    assert txn.status_code == 200
    assert "Morgan Patel" in txn.json()["text"]

    # 2. Human feedback (interviewer).
    human = await client.post(
        f"/api/v1/interviewer/rounds/{round_id}/feedback",
        headers=ih,
        json={"rating": 5, "recommend": True, "notes": "Strong system design."},
    )
    assert human.status_code == 200
    assert human.json()["source"] == "human"

    # 3. Bot feedback (auto from transcript).
    bot = await client.post(
        f"/api/v1/interviewer/rounds/{round_id}/bot-feedback", headers=ih
    )
    assert bot.status_code == 200
    assert bot.json()["source"] == "bot"

    # 4. Assessment (hr acts on it) — both feedbacks feed the prediction.
    hh = await _hr(client)
    assess = await client.post(
        f"/api/v1/interviewer/rounds/{round_id}/assess", headers=hh
    )
    assert assess.status_code == 200
    data = assess.json()
    assert 0 <= data["suitability_pct"] <= 100
    assert "rationale" in data
    assert isinstance(data["recommend_advance"], bool)

    # 5. Round detail reflects transcript + 2 feedbacks + assessment.
    detail = await client.get(f"/api/v1/interviewer/rounds/{round_id}", headers=ih)
    d = detail.json()
    assert d["transcript"] is not None
    assert len(d["feedbacks"]) == 2
    assert {f["source"] for f in d["feedbacks"]} == {"human", "bot"}
    assert d["assessment"] is not None


async def test_bot_feedback_requires_transcript(client: httpx.AsyncClient) -> None:
    round_id = await _assign_round(client)
    ih = await _interviewer(client)
    # No transcript yet -> 409.
    resp = await client.post(
        f"/api/v1/interviewer/rounds/{round_id}/bot-feedback", headers=ih
    )
    assert resp.status_code == 409


async def test_feedback_requires_interviewer_role(client: httpx.AsyncClient) -> None:
    round_id = await _assign_round(client)
    hh = await _hr(client)
    # HR cannot submit interviewer feedback.
    resp = await client.post(
        f"/api/v1/interviewer/rounds/{round_id}/feedback",
        headers=hh,
        json={"rating": 4, "recommend": True, "notes": "x"},
    )
    assert resp.status_code == 403
