"""Interviewer routes: assigned interviews, transcripts, feedback, assessment.

The interviewer sees only the rounds assigned to them. For each round they can
pull the (bot-generated) transcript, submit their own feedback, trigger the bot
feedback, and run the LLM assessment that predicts the candidate's suitability %
from the transcript plus BOTH feedbacks (human + bot). Keeping both feedbacks is
deliberate: the bot scales, the human supervises.

The assess endpoint is also exposed to recruiting (hr/admin) since they act on
the prediction; feedback submission is interviewer-only.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_token_payload, interviewer_user, recruiting_user
from app.db.models import (
    FEEDBACK_SOURCE_BOT,
    FEEDBACK_SOURCE_HUMAN,
    ROUND_STATUS_COMPLETED,
    Assessment,
    Feedback,
    InterviewRoundRec,
    Transcript,
)
from app.db.session import get_db
from app.services.assessment import FeedbackInput, get_assessment_provider
from app.services.integrations import transcription_service

router = APIRouter()

DbDep = Annotated[AsyncSession, Depends(get_db)]


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _load_round(db: AsyncSession, round_id: str) -> InterviewRoundRec:
    result = await db.execute(
        select(InterviewRoundRec)
        .where(InterviewRoundRec.id == round_id)
        .options(
            selectinload(InterviewRoundRec.feedbacks),
            selectinload(InterviewRoundRec.transcript),
            selectinload(InterviewRoundRec.assessment),
            selectinload(InterviewRoundRec.application),
        )
    )
    round_row = result.scalar_one_or_none()
    if round_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Round not found")
    return round_row


def _ensure_assigned(round_row: InterviewRoundRec, payload: dict) -> None:
    """Interviewers may only touch their own assigned rounds."""
    if payload.get("role") == "interviewer" and round_row.interviewer_user_id != payload.get("sub"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="This interview is not assigned to you.",
        )


# ==========================================================================
# Schemas
# ==========================================================================
class AssignmentOut(BaseModel):
    round_id: str
    index: int
    name: str
    type: str
    status: str
    meeting_url: str | None
    applicant_name: str
    applicant_email: str
    has_feedback: bool


class FeedbackOut(BaseModel):
    id: str
    source: str
    rating: int
    recommend: bool
    notes: str


class AssessmentOut(BaseModel):
    suitability_pct: int
    rationale: str
    recommend_advance: bool


class RoundDetailOut(BaseModel):
    round_id: str
    name: str
    type: str
    status: str
    applicant_name: str
    transcript: str | None
    feedbacks: list[FeedbackOut]
    assessment: AssessmentOut | None


# ==========================================================================
# Assignments
# ==========================================================================
@router.get("/interviewer/assignments", response_model=list[AssignmentOut])
async def my_assignments(
    db: DbDep, payload: dict = Depends(interviewer_user)
) -> list[AssignmentOut]:
    me = str(payload.get("sub", ""))
    result = await db.execute(
        select(InterviewRoundRec)
        .where(InterviewRoundRec.interviewer_user_id == me)
        .options(
            selectinload(InterviewRoundRec.application),
            selectinload(InterviewRoundRec.feedbacks),
        )
        .order_by(InterviewRoundRec.scheduled_at.desc().nullslast())
    )
    rounds = result.scalars().all()
    out: list[AssignmentOut] = []
    for r in rounds:
        app_row = r.application
        out.append(
            AssignmentOut(
                round_id=r.id,
                index=r.index,
                name=r.name,
                type=r.type,
                status=r.status,
                meeting_url=r.meeting_url,
                applicant_name=app_row.applicant_name if app_row else "",
                applicant_email=app_row.applicant_email if app_row else "",
                has_feedback=any(
                    f.source == FEEDBACK_SOURCE_HUMAN for f in r.feedbacks
                ),
            )
        )
    return out


@router.get("/interviewer/rounds/{round_id}", response_model=RoundDetailOut)
async def round_detail(
    round_id: str, db: DbDep, payload: dict = Depends(get_token_payload)
) -> RoundDetailOut:
    # interviewer (own) or hr/admin may view.
    if payload.get("role") not in ("interviewer", "hr", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Not permitted")
    round_row = await _load_round(db, round_id)
    _ensure_assigned(round_row, payload)
    return RoundDetailOut(
        round_id=round_row.id,
        name=round_row.name,
        type=round_row.type,
        status=round_row.status,
        applicant_name=round_row.application.applicant_name if round_row.application else "",
        transcript=round_row.transcript.text if round_row.transcript else None,
        feedbacks=[
            FeedbackOut(id=f.id, source=f.source, rating=f.rating,
                        recommend=f.recommend, notes=f.notes)
            for f in round_row.feedbacks
        ],
        assessment=(
            AssessmentOut(
                suitability_pct=round_row.assessment.suitability_pct,
                rationale=round_row.assessment.rationale,
                recommend_advance=round_row.assessment.recommend_advance,
            )
            if round_row.assessment
            else None
        ),
    )


# ==========================================================================
# Transcript
# ==========================================================================
class TranscriptOut(BaseModel):
    round_id: str
    text: str


@router.post("/interviewer/rounds/{round_id}/transcript", response_model=TranscriptOut)
async def generate_transcript(
    round_id: str, db: DbDep, payload: dict = Depends(get_token_payload)
) -> TranscriptOut:
    """Produce (or regenerate) the transcript for a round via the bot."""
    if payload.get("role") not in ("interviewer", "hr", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Not permitted")
    round_row = await _load_round(db, round_id)
    _ensure_assigned(round_row, payload)

    text = transcription_service.transcribe(
        candidate_name=round_row.application.applicant_name if round_row.application else "Candidate",
        round_name=round_row.name,
    )
    if round_row.transcript is None:
        db.add(Transcript(id=_new_id("txn"), round_id=round_row.id, text=text, source="bot"))
    else:
        round_row.transcript.text = text
    # Mark the round completed once we have a transcript.
    if round_row.status not in (ROUND_STATUS_COMPLETED,):
        round_row.status = ROUND_STATUS_COMPLETED
    await db.commit()
    return TranscriptOut(round_id=round_row.id, text=text)


# ==========================================================================
# Feedback (human + bot)
# ==========================================================================
class HumanFeedbackIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    recommend: bool
    notes: str = Field(default="", max_length=4000)


@router.post("/interviewer/rounds/{round_id}/feedback", response_model=FeedbackOut)
async def submit_human_feedback(
    round_id: str,
    body: HumanFeedbackIn,
    db: DbDep,
    payload: dict = Depends(interviewer_user),
) -> FeedbackOut:
    round_row = await _load_round(db, round_id)
    _ensure_assigned(round_row, payload)

    fb = Feedback(
        id=_new_id("fb"),
        round_id=round_row.id,
        source=FEEDBACK_SOURCE_HUMAN,
        author_user_id=str(payload.get("sub", "")) or None,
        rating=body.rating,
        recommend=body.recommend,
        notes=body.notes,
    )
    db.add(fb)
    await db.commit()
    return FeedbackOut(id=fb.id, source=fb.source, rating=fb.rating,
                       recommend=fb.recommend, notes=fb.notes)


@router.post("/interviewer/rounds/{round_id}/bot-feedback", response_model=FeedbackOut)
async def generate_bot_feedback(
    round_id: str, db: DbDep, payload: dict = Depends(get_token_payload)
) -> FeedbackOut:
    """Auto-generate the bot's feedback from the transcript. The bot derives a
    rating from transcript signals so there is always a machine opinion to sit
    alongside the human's."""
    if payload.get("role") not in ("interviewer", "hr", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Not permitted")
    round_row = await _load_round(db, round_id)
    _ensure_assigned(round_row, payload)

    transcript_text = round_row.transcript.text if round_row.transcript else ""
    if not transcript_text:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Generate the transcript before bot feedback.",
        )

    # The bot uses the assessment provider to judge the transcript alone, then
    # expresses that as a 1-5 rating + recommendation.
    judged = get_assessment_provider().judge_interview(transcript_text, [])
    # Map the transcript-only signal into a 1-5 rating.
    rating = max(1, min(5, round(judged.pct / 20)))
    fb = Feedback(
        id=_new_id("fb"),
        round_id=round_row.id,
        source=FEEDBACK_SOURCE_BOT,
        author_user_id=None,
        rating=rating,
        recommend=judged.pct >= 60,
        notes=(
            "Automated review from the interview transcript. "
            f"Transcript-based signal: {judged.pct}%."
        ),
    )
    db.add(fb)
    await db.commit()
    return FeedbackOut(id=fb.id, source=fb.source, rating=fb.rating,
                       recommend=fb.recommend, notes=fb.notes)


# ==========================================================================
# Assessment (the % prediction)
# ==========================================================================
@router.post("/interviewer/rounds/{round_id}/assess", response_model=AssessmentOut)
async def assess_round(
    round_id: str, db: DbDep, _p: dict = Depends(recruiting_user)
) -> AssessmentOut:
    """Run the LLM judgment over the transcript + all feedback, producing the
    candidate's suitability %. HR/admin action (they act on the prediction)."""
    round_row = await _load_round(db, round_id)

    transcript_text = round_row.transcript.text if round_row.transcript else ""
    feedback_inputs = [
        FeedbackInput(source=f.source, rating=f.rating, recommend=f.recommend, notes=f.notes)
        for f in round_row.feedbacks
    ]
    judged = get_assessment_provider().judge_interview(transcript_text, feedback_inputs)

    if round_row.assessment is None:
        db.add(
            Assessment(
                id=_new_id("asm"),
                round_id=round_row.id,
                suitability_pct=judged.pct,
                rationale=judged.rationale,
                recommend_advance=judged.recommend_advance,
            )
        )
    else:
        round_row.assessment.suitability_pct = judged.pct
        round_row.assessment.rationale = judged.rationale
        round_row.assessment.recommend_advance = judged.recommend_advance
    await db.commit()
    return AssessmentOut(
        suitability_pct=judged.pct,
        rationale=judged.rationale,
        recommend_advance=judged.recommend_advance,
    )
