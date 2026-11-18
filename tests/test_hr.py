"""HR route tests against the database-backed implementation.

These verify not just the response shapes but that mutations actually persist
and that the pipeline-status derivation rules still hold.
"""

import httpx

from tests.conftest import auth_header, login


async def _hr_token(client: httpx.AsyncClient) -> str:
    return await login(client, "hr@example.com")


async def test_list_candidates_shape(client: httpx.AsyncClient) -> None:
    token = await _hr_token(client)
    resp = await client.get("/api/v1/hr/candidates", headers=auth_header(token))
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    keys = {"id", "full_name", "email", "pipeline_status", "current_stage_name", "offer_generated_at"}
    assert keys.issubset(rows[0].keys())
    # Sorted by full_name: Alex before Jordan.
    assert rows[0]["full_name"] == "Alex Rivera"


async def test_current_stage_name_reflects_in_progress(client: httpx.AsyncClient) -> None:
    token = await _hr_token(client)
    resp = await client.get("/api/v1/hr/candidates", headers=auth_header(token))
    alex = next(r for r in resp.json() if r["id"] == "cand-1")
    # cand-1 has s3 "Team fit / values" in_progress in the seed.
    assert alex["current_stage_name"] == "Team fit / values"


async def test_patch_stage_persists_and_demotes_others(client: httpx.AsyncClient) -> None:
    token = await _hr_token(client)
    h = auth_header(token)

    # Move s4 to in_progress; s3 (previously in_progress) must become pending.
    resp = await client.patch(
        "/api/v1/hr/candidates/cand-1/interview-stages",
        headers=h,
        json={"stage_id": "s4", "status": "in_progress"},
    )
    assert resp.status_code == 200
    stages = {s["id"]: s["status"] for s in resp.json()}
    assert stages["s4"] == "in_progress"
    assert stages["s3"] == "pending"

    # Re-fetch to confirm it persisted (not just returned).
    again = await client.get("/api/v1/hr/candidates/cand-1/interview-stages", headers=h)
    persisted = {s["id"]: s["status"] for s in again.json()}
    assert persisted["s4"] == "in_progress"
    assert persisted["s3"] == "pending"


async def test_all_stages_done_sets_ready_for_offer(client: httpx.AsyncClient) -> None:
    token = await _hr_token(client)
    h = auth_header(token)
    for sid in ["s1", "s2", "s3", "s4", "s5"]:
        await client.patch(
            "/api/v1/hr/candidates/cand-1/interview-stages",
            headers=h,
            json={"stage_id": sid, "status": "completed"},
        )
    resp = await client.get("/api/v1/hr/candidates", headers=h)
    alex = next(r for r in resp.json() if r["id"] == "cand-1")
    assert alex["pipeline_status"] == "ready_for_offer"
    assert alex["current_stage_name"] == "Complete"


async def test_generate_offer_then_workflow_persists(client: httpx.AsyncClient) -> None:
    token = await _hr_token(client)
    h = auth_header(token)

    gen = await client.post(
        "/api/v1/hr/candidates/cand-1/offer-letter/generate", headers=h
    )
    assert gen.status_code == 200
    assert gen.json()["candidate_id"] == "cand-1"

    # offer_generated_at now shows up in the candidate summary.
    summary = await client.get("/api/v1/hr/candidates", headers=h)
    alex = next(r for r in summary.json() if r["id"] == "cand-1")
    assert alex["offer_generated_at"] is not None

    # Initiate a workflow, then confirm it appears in recent workflows.
    init = await client.post(
        "/api/v1/hr/workflows/onboarding/initiate",
        headers=h,
        json={"candidate_id": "cand-1"},
    )
    assert init.status_code == 200
    run_id = init.json()["workflow_run_id"]

    recent = await client.get("/api/v1/hr/workflows/recent", headers=h)
    assert any(r["workflow_run_id"] == run_id for r in recent.json())


async def test_unknown_candidate_404(client: httpx.AsyncClient) -> None:
    token = await _hr_token(client)
    resp = await client.get(
        "/api/v1/hr/candidates/does-not-exist/interview-stages",
        headers=auth_header(token),
    )
    assert resp.status_code == 404
