"""Idempotent database seeding.

Populates the data that used to live in module-level dicts/lists across the
route files, now as real rows with hashed passwords and relationships. Safe to
run repeatedly: if users already exist it does nothing. The demo passwords are
still "password" so existing logins keep working — but they are stored hashed.

Run standalone:  python -m app.db.seed
"""

import asyncio
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.models import (
    ROLE_ADMIN,
    ROLE_CANDIDATE,
    ROLE_HR,
    ROLE_INTERVIEWER,
    Application,
    Asset,
    AuditLog,
    Candidate,
    Employee,
    InterviewStage,
    InventoryItem,
    Job,
    User,
)
from app.db.session import SessionLocal, init_db

DEMO_PASSWORD = "password"


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


async def _already_seeded(db: AsyncSession) -> bool:
    result = await db.execute(select(User.id).limit(1))
    return result.first() is not None


async def seed(db: AsyncSession) -> None:
    if await _already_seeded(db):
        return

    pw = hash_password(DEMO_PASSWORD)

    users = [
        User(id="hr-1", email="hr@example.com", hashed_password=pw,
             display_name="Jamie Recruiter", role=ROLE_HR),
        User(id="cand-1", email="candidate@example.com", hashed_password=pw,
             display_name="Alex Rivera", role=ROLE_CANDIDATE),
        User(id="adm-1", email="admin@example.com", hashed_password=pw,
             display_name="Sam Admin", role=ROLE_ADMIN),
        User(id="int-1", email="interviewer@example.com", hashed_password=pw,
             display_name="Riley Interviewer", role=ROLE_INTERVIEWER),
    ]
    db.add_all(users)

    # --- Candidate 1: Alex Rivera (linked to the candidate user) ---
    cand1 = Candidate(
        id="cand-1",
        user_id="cand-1",
        full_name="Alex Rivera",
        email="candidate@example.com",
        job_title="Senior Software Engineer",
        salary_display="USD 145,000 base + bonus eligible",
        pipeline_status="offer_extended",
        onboarding_status="offer_extended",
        offer_available=True,
    )
    cand1.interview_stages = [
        InterviewStage(id="s1", name="Recruiter screen", status="completed", position=0,
                       scheduled_at=_dt("2026-03-18T15:00:00+00:00"),
                       notes="Strong communication; proceed to technical."),
        InterviewStage(id="s2", name="Technical interview", status="completed", position=1,
                       scheduled_at=_dt("2026-03-22T17:30:00+00:00"),
                       notes="Passed system design + coding."),
        InterviewStage(id="s3", name="Team fit / values", status="in_progress", position=2,
                       scheduled_at=_dt("2026-04-02T14:00:00+00:00")),
        InterviewStage(id="s4", name="Executive round", status="pending", position=3),
        InterviewStage(id="s5", name="Reference checks", status="pending", position=4),
    ]
    cand1.assets = [
        Asset(id="a1", name='Laptop 14"', category="hardware", status="pending_approval"),
        Asset(id="a2", name="Okta SSO", category="access", status="provisioned"),
    ]
    db.add(cand1)

    # --- Candidate 2: Jordan Lee (no login account) ---
    cand2 = Candidate(
        id="cand-2",
        user_id=None,
        full_name="Jordan Lee",
        email="j.lee@example.com",
        job_title="Product Designer",
        salary_display="USD 120,000 base",
        pipeline_status="interviewing",
        onboarding_status="in_progress",
        offer_available=False,
    )
    cand2.interview_stages = [
        InterviewStage(id="s6", name="Recruiter screen", status="completed", position=0),
        InterviewStage(id="s7", name="Technical interview", status="in_progress", position=1),
        InterviewStage(id="s8", name="Team fit / values", status="pending", position=2),
    ]
    cand2.assets = [
        Asset(id="a3", name="Badge / building access", category="facilities", status="not_requested"),
    ]
    db.add(cand2)

    # --- Admin: employees ---
    db.add_all([
        Employee(id="emp-1001", user_id="cand-1", full_name="Alex Rivera",
                 title="Senior Software Engineer", department="Engineering",
                 status="preboarding", start_date=date(2026, 5, 1)),
        Employee(id="emp-1002", user_id=None, full_name="Priya Shah",
                 title="People Partner", department="HR",
                 status="active", start_date=date(2025, 8, 12)),
    ])

    # --- Admin: inventory ---
    db.add_all([
        InventoryItem(sku="HW-LP-14", name='Laptop 14"', category="hardware",
                      in_stock=42, reserved=7, reorder_point=10),
        InventoryItem(sku="ACC-BDG", name="Building badge", category="facilities",
                      in_stock=500, reserved=120, reorder_point=200),
        InventoryItem(sku="SFT-OKTA", name="Okta seat", category="software",
                      in_stock=800, reserved=650, reorder_point=100),
    ])

    # --- Admin: audit logs ---
    db.add_all([
        AuditLog(id="aud-001", timestamp=_dt("2026-04-01T10:15:00+00:00"),
                 actor_email="admin@example.com", action="config.read",
                 resource="integration.sap", detail="Viewed SuccessFactors mock settings"),
        AuditLog(id="aud-002", timestamp=_dt("2026-04-02T14:22:00+00:00"),
                 actor_email="hr@example.com", action="candidate.stage_update",
                 resource="cand-1", detail="Marked technical interview completed"),
    ])

    # --- ATS: a sample open job + fresh applications to screen ---
    job1 = Job(
        id="job-1",
        title="Senior Software Engineer",
        department="Engineering",
        description=(
            "Build and scale our backend services. Own features end to end, "
            "collaborate with product, and mentor engineers."
        ),
        requirements=(
            "python, fastapi, sqlalchemy, postgresql, rest api, docker, "
            "system design, testing, ci/cd, aws"
        ),
        salary_display="USD 145,000 base + bonus eligible",
        shortlist_threshold=60,
        num_rounds=3,
        status="open",
    )
    db.add(job1)

    db.add_all([
        Application(
            id="app-1",
            job_id="job-1",
            candidate_id=None,
            applicant_name="Morgan Patel",
            applicant_email="morgan.patel@example.com",
            resume_text=(
                "Senior backend engineer with 7 years building REST APIs in "
                "Python and FastAPI. Deep SQLAlchemy and PostgreSQL experience, "
                "Docker, CI/CD on AWS, strong system design and testing habits."
            ),
            status="applied",
        ),
        Application(
            id="app-2",
            job_id="job-1",
            candidate_id=None,
            applicant_name="Casey Nguyen",
            applicant_email="casey.nguyen@example.com",
            resume_text=(
                "Frontend developer focused on React and design systems. Some "
                "Node.js scripting. Limited backend or database experience."
            ),
            status="applied",
        ),
    ])

    await db.commit()


async def main() -> None:
    await init_db()
    async with SessionLocal() as db:
        await seed(db)
    print("Seed complete.")


if __name__ == "__main__":
    asyncio.run(main())
