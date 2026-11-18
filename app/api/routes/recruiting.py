"""Recruiting / applicant-tracking routes (HR + admin).

Drives the pipeline: post jobs, receive applications, screen resumes with the
assessment service, run the call-bot availability round, assign interviewers
(which creates a Teams link and emails the candidate), then advance or reject
candidates round by round until an offer is generated.

All endpoints require the hr/admin role except public application submission.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import recruiting_user
from app.db.models import (
    APP_STATUS_APPLIED,
    APP_STATUS_INTERVIEWING,
    APP_STATUS_OFFER,
    APP_STATUS_REJECTED,
    APP_STATUS_SCREENING,
    APP_STATUS_SHORTLISTED,
    ROUND_STATUS_COMPLETED,
    ROUND_STATUS_FAILED,
    ROUND_STATUS_PASSED,
    ROUND_STATUS_PENDING,
    ROUND_STATUS_SCHEDULED,
    Application,
    Candidate,
    InterviewRoundRec,
    Job,
    User,
)
from app.db.session import get_db
from app.services.assessment import get_assessment_provider
from app.services.integrations import (
    call_bot_service,
    email_service,
    teams_service,
)

router = APIRouter()

DbDep = Annotated[AsyncSession, Depends(get_db)]


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# ==========================================================================
# Schemas
# ==========================================================================
class JobIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    department: str = ""
    description: str = ""
    requirements: str = ""
    salary_display: str = ""
    shortlist_threshold: int = Field(default=60, ge=0, le=100)
    num_rounds: int = Field(default=3, ge=1, le=10)


class JobPatch(BaseModel):
    shortlist_threshold: int | None = Field(default=None, ge=0, le=100)
    num_rounds: int | None = Field(default=None, ge=1, le=10)
    status: str | None = None


class JobOut(BaseModel):
    id: str
    title: str
    department: str
    description: str
    requirements: str
    salary_display: str
    shortlist_threshold: int
    num_rounds: int
    status: str
    application_count: int = 0


class ApplicationIn(BaseModel):
    job_id: str
    applicant_name: str = Field(min_length=1, max_length=255)
    applicant_email: EmailStr
    resume_text: str = Field(default="", max_length=20000)


class RoundOut(BaseModel):
    id: str
    index: int
    name: str
    type: str
    status: str
    scheduled_at: str | None
    meeting_url: str | None
    interviewer_user_id: str | None


class ApplicationOut(BaseModel):
    id: str
    job_id: str
    applicant_name: str
    applicant_email: str
    status: str
    screening_score: int | None
    screening_rationale: str | None
    current_round: int
    availability_confirmed: bool
    availability_notes: str | None
    rounds: list[RoundOut]


# ==========================================================================
# Helpers
# ==========================================================================
async def _get_job(db: AsyncSession, job_id: str) -> Job:
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


async def _get_application(db: AsyncSession, application_id: str) -> Application:
    result = await db.execute(
        select(Application)
        .where(Application.id == application_id)
        .options(selectinload(Application.rounds))
    )
    app_row = result.scalar_one_or_none()
    if app_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Application not found")
    return app_row


def _round_out(r: InterviewRoundRec) -> RoundOut:
    return RoundOut(
        id=r.id,
        index=r.index,
        name=r.name,
        type=r.type,
        status=r.status,
        scheduled_at=r.scheduled_at.isoformat() if r.scheduled_at else None,
        meeting_url=r.meeting_url,
        interviewer_user_id=r.interviewer_user_id,
    )


def _application_out(app_row: Application) -> ApplicationOut:
    return ApplicationOut(
        id=app_row.id,
        job_id=app_row.job_id,
        applicant_name=app_row.applicant_name,
        applicant_email=app_row.applicant_email,
        status=app_row.status,
        screening_score=app_row.screening_score,
        screening_rationale=app_row.screening_rationale,
        current_round=app_row.current_round,
        availability_confirmed=app_row.availability_confirmed,
        availability_notes=app_row.availability_notes,
        rounds=[_round_out(r) for r in sorted(app_row.rounds, key=lambda x: x.index)],
    )


# ==========================================================================
# Jobs
# ==========================================================================
@router.get("/recruiting/jobs", response_model=list[JobOut])
async def list_jobs(db: DbDep, _p: dict = Depends(recruiting_user)) -> list[JobOut]:
    result = await db.execute(
        select(Job).options(selectinload(Job.applications)).order_by(Job.title)
    )
    jobs = result.scalars().all()
    return [
        JobOut(
            id=j.id,
            title=j.title,
            department=j.department,
            description=j.description,
            requirements=j.requirements,
            salary_display=j.salary_display,
            shortlist_threshold=j.shortlist_threshold,
            num_rounds=j.num_rounds,
            status=j.status,
            application_count=len(j.applications),
        )
        for j in jobs
    ]


@router.post("/recruiting/jobs", response_model=JobOut, status_code=201)
async def create_job(body: JobIn, db: DbDep, _p: dict = Depends(recruiting_user)) -> JobOut:
    job = Job(
        id=_new_id("job"),
        title=body.title,
        department=body.department,
        description=body.description,
        requirements=body.requirements,
        salary_display=body.salary_display,
        shortlist_threshold=body.shortlist_threshold,
        num_rounds=body.num_rounds,
        status="open",
    )
    db.add(job)
    await db.commit()
    return JobOut(
        id=job.id, title=job.title, department=job.department,
        description=job.description, requirements=job.requirements,
        salary_display=job.salary_display, shortlist_threshold=job.shortlist_threshold,
        num_rounds=job.num_rounds, status=job.status, application_count=0,
    )


@router.patch("/recruiting/jobs/{job_id}", response_model=JobOut)
async def patch_job(
    job_id: str, body: JobPatch, db: DbDep, _p: dict = Depends(recruiting_user)
) -> JobOut:
    job = await _get_job(db, job_id)
    if body.shortlist_threshold is not None:
        job.shortlist_threshold = body.shortlist_threshold
    if body.num_rounds is not None:
        job.num_rounds = body.num_rounds
    if body.status is not None:
        job.status = body.status
    await db.commit()
    return JobOut(
        id=job.id, title=job.title, department=job.department,
        description=job.description, requirements=job.requirements,
        salary_display=job.salary_display, shortlist_threshold=job.shortlist_threshold,
        num_rounds=job.num_rounds, status=job.status,
    )


@router.get("/recruiting/jobs/{job_id}/applications", response_model=list[ApplicationOut])
async def list_applications(
    job_id: str, db: DbDep, _p: dict = Depends(recruiting_user)
) -> list[ApplicationOut]:
    await _get_job(db, job_id)
    result = await db.execute(
        select(Application)
        .where(Application.job_id == job_id)
        .options(selectinload(Application.rounds))
        .order_by(Application.screening_score.desc().nullslast(), Application.applicant_name)
    )
    return [_application_out(a) for a in result.scalars().all()]


# ==========================================================================
# Applications
# ==========================================================================
@router.post("/recruiting/applications", response_model=ApplicationOut, status_code=201)
async def submit_application(body: ApplicationIn, db: DbDep) -> ApplicationOut:
    """Public: a candidate submits an application to a job."""
    await _get_job(db, body.job_id)
    app_row = Application(
        id=_new_id("app"),
        job_id=body.job_id,
        candidate_id=None,
        applicant_name=body.applicant_name,
        applicant_email=str(body.applicant_email).lower(),
        resume_text=body.resume_text,
        status=APP_STATUS_APPLIED,
    )
    db.add(app_row)
    await db.commit()
    app_row = await _get_application(db, app_row.id)
    return _application_out(app_row)


class ScreenResult(BaseModel):
    application_id: str
    screening_score: int
    screening_rationale: str
    status: str
    shortlisted: bool


@router.post("/recruiting/applications/{application_id}/screen", response_model=ScreenResult)
async def screen_application(
    application_id: str, db: DbDep, _p: dict = Depends(recruiting_user)
) -> ScreenResult:
    app_row = await _get_application(db, application_id)
    job = await _get_job(db, app_row.job_id)

    result = get_assessment_provider().score_resume(app_row.resume_text, job.requirements)
    app_row.screening_score = result.pct
    app_row.screening_rationale = result.rationale

    shortlisted = result.pct >= job.shortlist_threshold
    app_row.status = APP_STATUS_SHORTLISTED if shortlisted else APP_STATUS_SCREENING
    await db.commit()
    return ScreenResult(
        application_id=app_row.id,
        screening_score=result.pct,
        screening_rationale=result.rationale,
        status=app_row.status,
        shortlisted=shortlisted,
    )


class CallBotResult(BaseModel):
    application_id: str
    availability_confirmed: bool
    availability_notes: str
    round_id: str


@router.post("/recruiting/applications/{application_id}/call-bot", response_model=CallBotResult)
async def run_call_bot(
    application_id: str, db: DbDep, _p: dict = Depends(recruiting_user)
) -> CallBotResult:
    """Bot 'calls' a shortlisted candidate to confirm availability, and records
    a completed call_bot round."""
    app_row = await _get_application(db, application_id)
    if app_row.status not in (APP_STATUS_SHORTLISTED, APP_STATUS_INTERVIEWING):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Candidate must be shortlisted before the availability call.",
        )

    result = call_bot_service.call_for_availability(
        name=app_row.applicant_name, email=app_row.applicant_email
    )
    app_row.availability_confirmed = result.confirmed
    app_row.availability_notes = result.notes

    call_round = InterviewRoundRec(
        id=_new_id("rnd"),
        application_id=app_row.id,
        index=0,
        name="Availability call (bot)",
        type="call_bot",
        status=ROUND_STATUS_COMPLETED,
        scheduled_at=_now(),
    )
    db.add(call_round)
    if app_row.status == APP_STATUS_SHORTLISTED:
        app_row.status = APP_STATUS_INTERVIEWING
    await db.commit()
    return CallBotResult(
        application_id=app_row.id,
        availability_confirmed=result.confirmed,
        availability_notes=result.notes,
        round_id=call_round.id,
    )


class AssignInterviewerIn(BaseModel):
    interviewer_user_id: str
    round_name: str = "Interview"
    round_type: str = "interview"


class AssignInterviewerResult(BaseModel):
    round_id: str
    index: int
    interviewer_user_id: str
    meeting_url: str
    email_to: str


@router.post(
    "/recruiting/applications/{application_id}/assign-interviewer",
    response_model=AssignInterviewerResult,
)
async def assign_interviewer(
    application_id: str,
    body: AssignInterviewerIn,
    db: DbDep,
    _p: dict = Depends(recruiting_user),
) -> AssignInterviewerResult:
    """One click: create the next interview round, generate a Teams link, assign
    the interviewer, and email the candidate the join link."""
    app_row = await _get_application(db, application_id)
    job = await _get_job(db, app_row.job_id)

    interviewer = await db.get(User, body.interviewer_user_id)
    if interviewer is None or interviewer.role != "interviewer":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="interviewer_user_id must reference an interviewer user.",
        )

    # Next human interview round index (call_bot round is index 0).
    human_rounds = [r for r in app_row.rounds if r.type != "call_bot"]
    next_index = len(human_rounds) + 1
    if next_index > job.num_rounds:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"All {job.num_rounds} rounds already created for this job.",
        )

    meeting_url = teams_service.create_meeting(
        subject=f"{job.title} — {body.round_name} with {app_row.applicant_name}"
    )
    new_round = InterviewRoundRec(
        id=_new_id("rnd"),
        application_id=app_row.id,
        index=next_index,
        name=body.round_name,
        type=body.round_type,
        status=ROUND_STATUS_SCHEDULED,
        scheduled_at=_now(),
        meeting_url=meeting_url,
        interviewer_user_id=interviewer.id,
    )
    db.add(new_round)
    app_row.status = APP_STATUS_INTERVIEWING
    app_row.current_round = next_index

    email_service.send(
        to=app_row.applicant_email,
        subject=f"Your {job.title} interview — {body.round_name}",
        body=(
            f"Hi {app_row.applicant_name},\n\n"
            f"Your {body.round_name} for the {job.title} role is scheduled. "
            f"Join via Microsoft Teams:\n{meeting_url}\n\n"
            f"Your interviewer will be {interviewer.display_name}.\n\n"
            "Best,\nRecruiting Team"
        ),
    )
    await db.commit()
    return AssignInterviewerResult(
        round_id=new_round.id,
        index=next_index,
        interviewer_user_id=interviewer.id,
        meeting_url=meeting_url,
        email_to=app_row.applicant_email,
    )


class RoundDecision(BaseModel):
    decision: Literal["advance", "reject"]


class DecisionResult(BaseModel):
    round_id: str
    round_status: str
    application_status: str
    offer_generated: bool


@router.post("/recruiting/rounds/{round_id}/decision", response_model=DecisionResult)
async def decide_round(
    round_id: str, body: RoundDecision, db: DbDep, _p: dict = Depends(recruiting_user)
) -> DecisionResult:
    """HR decision on a round: advance (pass) or reject. Rejecting ends the
    pipeline; passing the final round moves the candidate to an offer."""
    round_row = await db.get(InterviewRoundRec, round_id)
    if round_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Round not found")
    app_row = await _get_application(db, round_row.application_id)
    job = await _get_job(db, app_row.job_id)

    offer_generated = False
    if body.decision == "reject":
        round_row.status = ROUND_STATUS_FAILED
        app_row.status = APP_STATUS_REJECTED
    else:
        round_row.status = ROUND_STATUS_PASSED
        # Count passed human rounds. `app_row.rounds` includes round_row (same
        # session), so its just-set PASSED status is already reflected here.
        human_rounds_passed = sum(
            1 for r in app_row.rounds
            if r.type != "call_bot" and r.status == ROUND_STATUS_PASSED
        )
        if human_rounds_passed >= job.num_rounds:
            app_row.status = APP_STATUS_OFFER
            offer_generated = True
            # Create a candidate offer record, reusing the onboarding flow.
            await _create_offer_for_application(db, app_row, job)

    await db.commit()
    return DecisionResult(
        round_id=round_row.id,
        round_status=round_row.status,
        application_status=app_row.status,
        offer_generated=offer_generated,
    )


@router.post("/recruiting/applications/{application_id}/reject", response_model=ApplicationOut)
async def reject_application(
    application_id: str, db: DbDep, _p: dict = Depends(recruiting_user)
) -> ApplicationOut:
    """One-click reject at any stage."""
    app_row = await _get_application(db, application_id)
    app_row.status = APP_STATUS_REJECTED
    await db.commit()
    app_row = await _get_application(db, application_id)
    return _application_out(app_row)


async def _create_offer_for_application(
    db: AsyncSession, app_row: Application, job: Job
) -> None:
    """Create (or update) a Candidate offer record for a candidate who cleared
    all rounds, reusing the onboarding Candidate model + PDF offer pipeline."""
    candidate: Candidate | None = None
    if app_row.candidate_id:
        candidate = await db.get(Candidate, app_row.candidate_id)
    if candidate is None:
        candidate = Candidate(
            id=_new_id("cand"),
            user_id=None,
            full_name=app_row.applicant_name,
            email=app_row.applicant_email,
            job_title=job.title,
            salary_display=job.salary_display,
            pipeline_status="offer_extended",
            onboarding_status="offer_extended",
            offer_available=True,
            offer_generated_at=_now(),
            offer_document_format="pdf",
        )
        db.add(candidate)
        app_row.candidate_id = candidate.id
    else:
        candidate.offer_available = True
        candidate.offer_generated_at = _now()
        candidate.job_title = job.title
        candidate.salary_display = job.salary_display
