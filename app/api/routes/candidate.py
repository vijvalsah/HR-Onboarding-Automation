from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import candidate_user
from app.db.models import Candidate
from app.db.session import get_db
from app.services.chat import get_chat_provider
from app.services.documents import build_offer_letter_pdf

router = APIRouter()

DbDep = Annotated[AsyncSession, Depends(get_db)]

# Stages considered still-active when locating the candidate's current position.
_ACTIVE_STATES = ("in_progress", "pending")


class InterviewStageOut(BaseModel):
    id: str
    name: str
    status: str
    scheduled_at: str | None = None
    notes: str | None = None


class OfferSummaryOut(BaseModel):
    available: bool
    job_title: str
    salary_display: str
    document_format: str = Field(
        default="pdf",
        description="Format of the downloadable offer document.",
    )


class CandidateMeOut(BaseModel):
    candidate_id: str
    full_name: str
    email: str
    onboarding_status: str
    current_stage_index: int
    interview_stages: list[InterviewStageOut]
    offer: OfferSummaryOut


async def _load_own_candidate(db: AsyncSession, payload: dict) -> Candidate:
    """Load the candidate profile linked to the logged-in user.

    Uses the JWT subject (user id) to find the candidate that belongs to *this*
    user — so each candidate sees only their own data, unlike the old mock that
    returned one fixed profile for everyone.
    """
    user_id = str(payload.get("sub", ""))
    result = await db.execute(
        select(Candidate)
        .where(Candidate.user_id == user_id)
        .options(selectinload(Candidate.interview_stages))
    )
    candidate = result.scalar_one_or_none()
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No candidate profile is linked to this account.",
        )
    return candidate


def _current_stage_index(stages: list) -> int:
    """Index of the first in-progress stage, else first pending, else last."""
    for i, s in enumerate(stages):
        if s.status == "in_progress":
            return i
    for i, s in enumerate(stages):
        if s.status == "pending":
            return i
    return max(len(stages) - 1, 0)


def _to_me_out(candidate: Candidate) -> CandidateMeOut:
    stages = list(candidate.interview_stages)
    return CandidateMeOut(
        candidate_id=candidate.id,
        full_name=candidate.full_name,
        email=candidate.email,
        onboarding_status=candidate.onboarding_status,
        current_stage_index=_current_stage_index(stages),
        interview_stages=[
            InterviewStageOut(
                id=s.id,
                name=s.name,
                status=s.status,
                scheduled_at=s.scheduled_at.isoformat() if s.scheduled_at else None,
                notes=s.notes,
            )
            for s in stages
        ],
        offer=OfferSummaryOut(
            available=candidate.offer_available,
            job_title=candidate.job_title,
            salary_display=candidate.salary_display,
            document_format=candidate.offer_document_format,
        ),
    )


@router.get("/candidate/me", response_model=CandidateMeOut)
async def get_me(
    db: DbDep,
    payload: dict = Depends(candidate_user),
) -> CandidateMeOut:
    candidate = await _load_own_candidate(db, payload)
    return _to_me_out(candidate)


@router.get("/candidate/offer-letter")
async def download_offer_letter(
    db: DbDep,
    payload: dict = Depends(candidate_user),
) -> Response:
    candidate = await _load_own_candidate(db, payload)
    if not candidate.offer_available:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No offer letter is available yet.",
        )
    pdf_bytes = build_offer_letter_pdf(
        full_name=candidate.full_name,
        job_title=candidate.job_title,
        salary_display=candidate.salary_display,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="offer-letter.pdf"',
        },
    )


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    reply: str


@router.post("/candidate/chat", response_model=ChatResponse)
async def candidate_chat(
    body: ChatRequest,
    payload: dict = Depends(candidate_user),
) -> ChatResponse:
    _ = payload
    provider = get_chat_provider()
    reply = await provider.complete(body.message)
    return ChatResponse(reply=reply)
