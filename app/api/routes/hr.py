from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import hr_user
from app.db.models import Asset, Candidate, InterviewStage, WorkflowRun
from app.db.session import get_db

router = APIRouter()

DbDep = Annotated[AsyncSession, Depends(get_db)]

# Stages considered "done" for the purpose of deriving pipeline status.
_DONE_STATES = frozenset({"completed", "skipped"})


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _current_stage_name(stages: list[InterviewStage]) -> str:
    """First in-progress stage, else first pending, else 'Complete'.

    Stages arrive ordered by position (relationship order_by), so the first
    match reflects pipeline order.
    """
    for s in stages:
        if s.status == "in_progress":
            return s.name
    for s in stages:
        if s.status == "pending":
            return s.name
    return "Complete"


async def _get_candidate(db: AsyncSession, candidate_id: str) -> Candidate:
    """Load a candidate with interview stages eager-loaded (avoids N+1)."""
    result = await db.execute(
        select(Candidate)
        .where(Candidate.id == candidate_id)
        .options(selectinload(Candidate.interview_stages))
    )
    candidate = result.scalar_one_or_none()
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    return candidate


class HRCandidateSummary(BaseModel):
    id: str
    full_name: str
    email: str
    pipeline_status: str
    current_stage_name: str
    offer_generated_at: str | None


@router.get("/hr/candidates", response_model=list[HRCandidateSummary])
async def list_candidates(
    db: DbDep,
    _payload: dict = Depends(hr_user),
) -> list[HRCandidateSummary]:
    result = await db.execute(
        select(Candidate).options(selectinload(Candidate.interview_stages))
    )
    candidates = result.scalars().all()
    out = [
        HRCandidateSummary(
            id=c.id,
            full_name=c.full_name,
            email=c.email,
            pipeline_status=c.pipeline_status,
            current_stage_name=_current_stage_name(list(c.interview_stages)),
            offer_generated_at=c.offer_generated_at.isoformat() if c.offer_generated_at else None,
        )
        for c in candidates
    ]
    return sorted(out, key=lambda r: r.full_name.lower())


class InterviewStagePatch(BaseModel):
    stage_id: str
    status: Literal["pending", "in_progress", "completed", "skipped"]


class InterviewStageOut(BaseModel):
    id: str
    name: str
    status: str


@router.get(
    "/hr/candidates/{candidate_id}/interview-stages",
    response_model=list[InterviewStageOut],
)
async def get_interview_stages(
    candidate_id: str,
    db: DbDep,
    _payload: dict = Depends(hr_user),
) -> list[InterviewStageOut]:
    candidate = await _get_candidate(db, candidate_id)
    return [
        InterviewStageOut(id=s.id, name=s.name, status=s.status)
        for s in candidate.interview_stages
    ]


@router.patch(
    "/hr/candidates/{candidate_id}/interview-stages",
    response_model=list[InterviewStageOut],
)
async def patch_interview_stage(
    candidate_id: str,
    body: InterviewStagePatch,
    db: DbDep,
    _payload: dict = Depends(hr_user),
) -> list[InterviewStageOut]:
    candidate = await _get_candidate(db, candidate_id)
    stages = list(candidate.interview_stages)

    target = next((s for s in stages if s.id == body.stage_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Interview stage not found")

    target.status = body.status

    # Only one stage may be in progress at a time: demote any others.
    if body.status == "in_progress":
        for s in stages:
            if s.id != target.id and s.status == "in_progress":
                s.status = "pending"

    # Derive candidate pipeline status from the stage set.
    if all(s.status in _DONE_STATES for s in stages):
        candidate.pipeline_status = "ready_for_offer"
    elif any(s.status == "in_progress" for s in stages):
        candidate.pipeline_status = "interviewing"

    await db.commit()
    return [InterviewStageOut(id=s.id, name=s.name, status=s.status) for s in stages]


class OfferGenerateResponse(BaseModel):
    message: str
    generated_at: str
    candidate_id: str


@router.post(
    "/hr/candidates/{candidate_id}/offer-letter/generate",
    response_model=OfferGenerateResponse,
)
async def generate_offer_letter(
    candidate_id: str,
    db: DbDep,
    _payload: dict = Depends(hr_user),
) -> OfferGenerateResponse:
    candidate = await _get_candidate(db, candidate_id)
    ts = _now()
    candidate.offer_generated_at = ts
    candidate.offer_available = True
    await db.commit()
    return OfferGenerateResponse(
        message="Offer letter generated. The candidate can now download the PDF.",
        generated_at=ts.isoformat(),
        candidate_id=candidate_id,
    )


class AssetRow(BaseModel):
    id: str
    name: str
    category: str
    status: str
    updated_at: str


@router.get(
    "/hr/candidates/{candidate_id}/assets",
    response_model=list[AssetRow],
)
async def list_assets(
    candidate_id: str,
    db: DbDep,
    _payload: dict = Depends(hr_user),
) -> list[AssetRow]:
    # Ensure candidate exists (404 otherwise), then read its assets.
    await _get_candidate(db, candidate_id)
    result = await db.execute(select(Asset).where(Asset.candidate_id == candidate_id))
    assets = result.scalars().all()
    return [
        AssetRow(
            id=a.id,
            name=a.name,
            category=a.category,
            status=a.status,
            updated_at=a.updated_at.isoformat(),
        )
        for a in assets
    ]


class OnboardingInitiateBody(BaseModel):
    candidate_id: str = Field(min_length=1)


class OnboardingInitiateResponse(BaseModel):
    workflow_run_id: str
    status: Literal["started", "already_running"]
    started_at: str
    candidate_id: str


@router.post(
    "/hr/workflows/onboarding/initiate",
    response_model=OnboardingInitiateResponse,
)
async def initiate_onboarding(
    body: OnboardingInitiateBody,
    db: DbDep,
    _payload: dict = Depends(hr_user),
) -> OnboardingInitiateResponse:
    await _get_candidate(db, body.candidate_id)
    run_id = f"wf-{uuid.uuid4().hex[:12]}"
    ts = _now()
    db.add(
        WorkflowRun(
            id=run_id,
            candidate_id=body.candidate_id,
            type="onboarding",
            started_at=ts,
        )
    )
    await db.commit()
    return OnboardingInitiateResponse(
        workflow_run_id=run_id,
        status="started",
        started_at=ts.isoformat(),
        candidate_id=body.candidate_id,
    )


@router.get("/hr/workflows/recent", response_model=list[dict[str, Any]])
async def recent_workflows(
    db: DbDep,
    _payload: dict = Depends(hr_user),
    limit: int = 10,
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(WorkflowRun).order_by(WorkflowRun.started_at.desc()).limit(limit)
    )
    runs = result.scalars().all()
    return [
        {
            "workflow_run_id": r.id,
            "candidate_id": r.candidate_id,
            "started_at": r.started_at.isoformat(),
            "type": r.type,
        }
        for r in runs
    ]
