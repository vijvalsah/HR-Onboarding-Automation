"""ORM models.

Field names mirror the JSON the API already returns so the frontend contract
is unchanged. Relationships replace the previous ad-hoc nesting in dict literals:
a Candidate now owns its InterviewStages and Assets with real foreign keys.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

# --- Role values kept in sync with app.api.deps role guards ---
ROLE_CANDIDATE = "candidate"
ROLE_HR = "hr"
ROLE_ADMIN = "admin"
ROLE_INTERVIEWER = "interviewer"

# --- ATS workflow status/enum values (plain strings for SQLite portability) ---
APP_STATUS_APPLIED = "applied"
APP_STATUS_SCREENING = "screening"
APP_STATUS_SHORTLISTED = "shortlisted"
APP_STATUS_REJECTED = "rejected"
APP_STATUS_INTERVIEWING = "interviewing"
APP_STATUS_OFFER = "offer"

ROUND_STATUS_PENDING = "pending"
ROUND_STATUS_SCHEDULED = "scheduled"
ROUND_STATUS_COMPLETED = "completed"
ROUND_STATUS_PASSED = "passed"
ROUND_STATUS_FAILED = "failed"

FEEDBACK_SOURCE_HUMAN = "human"
FEEDBACK_SOURCE_BOT = "bot"


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)

    # A candidate user optionally has a candidate profile.
    candidate: Mapped[Candidate | None] = relationship(
        back_populates="user",
        uselist=False,
    )


class Candidate(Base, TimestampMixin):
    __tablename__ = "candidates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    job_title: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    salary_display: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    pipeline_status: Mapped[str] = mapped_column(String(64), default="interviewing", nullable=False)
    onboarding_status: Mapped[str] = mapped_column(String(64), default="in_progress", nullable=False)
    offer_available: Mapped[bool] = mapped_column(default=False, nullable=False)
    offer_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Format of the downloadable offer document. "pdf" now that the PDF pipeline
    # is live; kept as a column so it is data-driven rather than hardcoded.
    offer_document_format: Mapped[str] = mapped_column(
        String(8), default="pdf", nullable=False
    )

    user: Mapped[User | None] = relationship(back_populates="candidate")
    interview_stages: Mapped[list[InterviewStage]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
        order_by="InterviewStage.position",
    )
    assets: Mapped[list[Asset]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
    )


class InterviewStage(Base, TimestampMixin):
    __tablename__ = "interview_stages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    candidate: Mapped[Candidate] = relationship(back_populates="interview_stages")


class Asset(Base, TimestampMixin):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), default="not_requested", nullable=False)

    candidate: Mapped[Candidate] = relationship(back_populates="assets")


class Employee(Base, TimestampMixin):
    __tablename__ = "employees"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    department: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class InventoryItem(Base, TimestampMixin):
    __tablename__ = "inventory_items"

    sku: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    in_stock: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reorder_point: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    actor_email: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    type: Mapped[str] = mapped_column(String(64), default="onboarding", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ==========================================================================
# ATS / interview workflow models
# ==========================================================================


class Job(Base, TimestampMixin):
    """A job opening with its description, requirements, and pipeline config."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    department: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Comma/newline-separated requirement keywords used by resume screening.
    requirements: Mapped[str] = mapped_column(Text, default="", nullable=False)
    salary_display: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    # Minimum screening suitability % to auto-shortlist a candidate.
    shortlist_threshold: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    # How many interview rounds this job runs (HR-configurable).
    num_rounds: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)

    applications: Mapped[list[Application]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )


class Application(Base, TimestampMixin):
    """A candidate's application to a specific job — the pipeline unit."""

    __tablename__ = "applications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("candidates.id", ondelete="SET NULL"), index=True, nullable=True
    )
    applicant_name: Mapped[str] = mapped_column(String(255), nullable=False)
    applicant_email: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    resume_text: Mapped[str] = mapped_column(Text, default="", nullable=False)

    status: Mapped[str] = mapped_column(String(32), default="applied", nullable=False)
    # Resume-vs-JD suitability (0-100), set by the screening step.
    screening_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    screening_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 1-based index of the round the candidate is currently in (0 = not started).
    current_round: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Availability captured by the call-bot screening round.
    availability_confirmed: Mapped[bool] = mapped_column(default=False, nullable=False)
    availability_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    job: Mapped[Job] = relationship(back_populates="applications")
    candidate: Mapped[Candidate | None] = relationship()
    rounds: Mapped[list[InterviewRoundRec]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="InterviewRoundRec.index",
    )


class InterviewRoundRec(Base, TimestampMixin):
    """One interview round for an application.

    Named InterviewRoundRec to avoid clashing with the legacy InterviewStage.
    """

    __tablename__ = "interview_rounds"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True, nullable=False
    )
    index: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    # e.g. "call_bot", "technical", "hr", "executive".
    type: Mapped[str] = mapped_column(String(64), default="interview", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    meeting_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    interviewer_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    application: Mapped[Application] = relationship(back_populates="rounds")
    interviewer: Mapped[User | None] = relationship()
    feedbacks: Mapped[list[Feedback]] = relationship(
        back_populates="round",
        cascade="all, delete-orphan",
    )
    transcript: Mapped[Transcript | None] = relationship(
        back_populates="round",
        cascade="all, delete-orphan",
        uselist=False,
    )
    assessment: Mapped[Assessment | None] = relationship(
        back_populates="round",
        cascade="all, delete-orphan",
        uselist=False,
    )


class Feedback(Base, TimestampMixin):
    """Feedback on a round — from the human interviewer or the bot."""

    __tablename__ = "feedbacks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    round_id: Mapped[str] = mapped_column(
        ForeignKey("interview_rounds.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # "human" or "bot".
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    author_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 1-5 rating.
    rating: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    recommend: Mapped[bool] = mapped_column(default=False, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    round: Mapped[InterviewRoundRec] = relationship(back_populates="feedbacks")


class Transcript(Base, TimestampMixin):
    """Interview transcript produced by the transcription bot."""

    __tablename__ = "transcripts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    round_id: Mapped[str] = mapped_column(
        ForeignKey("interview_rounds.id", ondelete="CASCADE"), index=True, nullable=False
    )
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="bot", nullable=False)

    round: Mapped[InterviewRoundRec] = relationship(back_populates="transcript")


class Assessment(Base, TimestampMixin):
    """LLM judgment for a round: suitability % + rationale + advance suggestion."""

    __tablename__ = "assessments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    round_id: Mapped[str] = mapped_column(
        ForeignKey("interview_rounds.id", ondelete="CASCADE"), index=True, nullable=False
    )
    suitability_pct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    recommend_advance: Mapped[bool] = mapped_column(default=False, nullable=False)

    round: Mapped[InterviewRoundRec] = relationship(back_populates="assessment")
