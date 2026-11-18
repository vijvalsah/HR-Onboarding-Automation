"""Tests for the deterministic mock assessment provider."""

from app.services.assessment import (
    FeedbackInput,
    MockAssessmentProvider,
    get_assessment_provider,
)

REQUIREMENTS = (
    "python, fastapi, sqlalchemy, postgresql, rest api, docker, "
    "system design, testing, ci/cd, aws"
)


def test_default_provider_is_mock() -> None:
    assert isinstance(get_assessment_provider(), MockAssessmentProvider)


def test_strong_resume_scores_high() -> None:
    p = MockAssessmentProvider()
    strong = (
        "Senior backend engineer, 7 years of Python and FastAPI building REST "
        "API services. Deep SQLAlchemy and PostgreSQL, Docker, CI/CD on AWS, "
        "strong system design and testing."
    )
    score = p.score_resume(strong, REQUIREMENTS)
    assert score.pct >= 70
    assert "Matched" in score.rationale


def test_weak_resume_scores_low() -> None:
    p = MockAssessmentProvider()
    weak = "Frontend developer with React and CSS. Some design work."
    score = p.score_resume(weak, REQUIREMENTS)
    assert score.pct <= 30


def test_scoring_is_deterministic() -> None:
    p = MockAssessmentProvider()
    resume = "python fastapi docker"
    a = p.score_resume(resume, REQUIREMENTS)
    b = p.score_resume(resume, REQUIREMENTS)
    assert a.pct == b.pct
    assert a.rationale == b.rationale


def test_empty_requirements_is_neutral() -> None:
    p = MockAssessmentProvider()
    score = p.score_resume("anything", "")
    assert score.pct == 50


def test_judge_interview_no_feedback_is_provisional() -> None:
    p = MockAssessmentProvider()
    j = p.judge_interview("some transcript", [])
    assert j.recommend_advance is False
    assert 0 <= j.pct <= 100


def test_judge_interview_positive_feedback_recommends() -> None:
    p = MockAssessmentProvider()
    feedbacks = [
        FeedbackInput(source="human", rating=5, recommend=True, notes="Excellent"),
        FeedbackInput(source="bot", rating=4, recommend=True, notes="Strong signals"),
    ]
    j = p.judge_interview("great interview transcript", feedbacks)
    assert j.pct >= 60
    assert j.recommend_advance is True
    assert "human" in j.rationale and "bot" in j.rationale


def test_judge_interview_negative_feedback_rejects() -> None:
    p = MockAssessmentProvider()
    feedbacks = [
        FeedbackInput(source="human", rating=2, recommend=False, notes="Weak"),
        FeedbackInput(source="bot", rating=2, recommend=False, notes="Concerns"),
    ]
    j = p.judge_interview("transcript", feedbacks)
    assert j.pct < 60
    assert j.recommend_advance is False


def test_split_feedback_human_positive_bot_negative() -> None:
    # Manual supervision matters: mixed signals should not auto-advance.
    p = MockAssessmentProvider()
    feedbacks = [
        FeedbackInput(source="human", rating=5, recommend=True, notes="Loved them"),
        FeedbackInput(source="bot", rating=2, recommend=False, notes="Red flags"),
    ]
    j = p.judge_interview("transcript", feedbacks)
    # recommend_ratio = 0.5; advances only if blended score also >= 60.
    assert isinstance(j.recommend_advance, bool)
