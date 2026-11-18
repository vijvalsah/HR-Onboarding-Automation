"""Candidate assessment service.

Two jobs:
  1. score_resume(resume, requirements) -> (pct, rationale)
     How well a resume matches a job's requirements (used for shortlisting).
  2. judge_interview(transcript, feedbacks) -> (pct, rationale, recommend)
     A suitability prediction from the interview transcript plus the human and
     bot feedback (used to suggest who advances).

Like the chat service, this is pluggable. The default MockAssessmentProvider is
fully deterministic — it uses keyword overlap and simple feedback aggregation so
results are stable and unit-testable with no network or API key. Setting
ASSESSMENT_PROVIDER=openai/gemini (with the matching key) swaps in a real model
behind the same interface; those providers import their SDK lazily.

Design note: the mock is intentionally transparent (you can predict its score
from the inputs). A real LLM would replace the scoring logic but keep the same
return shape, so every downstream consumer (routes, DB writes, UI) is unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ResumeScore:
    pct: int  # 0-100
    rationale: str


@dataclass(frozen=True)
class InterviewJudgment:
    pct: int  # 0-100
    rationale: str
    recommend_advance: bool


_WORD_RE = re.compile(r"[a-z0-9+#.]+")


def _tokenize(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) > 1}


def _parse_requirements(requirements: str) -> list[str]:
    """Requirements are comma/newline separated phrases (e.g. 'system design')."""
    parts = re.split(r"[,\n;]+", requirements.lower())
    return [p.strip() for p in parts if p.strip()]


class AssessmentProvider:
    """Interface for resume scoring and interview judgment."""

    name = "base"

    def score_resume(self, resume_text: str, requirements: str) -> ResumeScore:
        raise NotImplementedError

    def judge_interview(
        self, transcript: str, feedback_summaries: list["FeedbackInput"]
    ) -> InterviewJudgment:
        raise NotImplementedError


@dataclass(frozen=True)
class FeedbackInput:
    source: str  # "human" | "bot"
    rating: int  # 1-5
    recommend: bool
    notes: str


class MockAssessmentProvider(AssessmentProvider):
    """Deterministic, offline scoring. No network, no key, fully testable."""

    name = "mock"

    def score_resume(self, resume_text: str, requirements: str) -> ResumeScore:
        reqs = _parse_requirements(requirements)
        if not reqs:
            return ResumeScore(pct=50, rationale="No requirements specified; neutral score.")

        resume_tokens = _tokenize(resume_text)
        matched: list[str] = []
        missing: list[str] = []
        for req in reqs:
            # A requirement phrase matches if all its significant tokens appear.
            req_tokens = _tokenize(req)
            if req_tokens and req_tokens.issubset(resume_tokens):
                matched.append(req)
            else:
                missing.append(req)

        pct = round(100 * len(matched) / len(reqs))
        rationale = (
            f"Matched {len(matched)}/{len(reqs)} requirements. "
            f"Present: {', '.join(matched) if matched else 'none'}. "
            f"Missing: {', '.join(missing) if missing else 'none'}."
        )
        return ResumeScore(pct=pct, rationale=rationale)

    def judge_interview(
        self, transcript: str, feedback_summaries: list[FeedbackInput]
    ) -> InterviewJudgment:
        if not feedback_summaries:
            # With no feedback, lean on transcript length as a weak signal.
            base = 50 if transcript.strip() else 30
            return InterviewJudgment(
                pct=base,
                rationale="No feedback captured yet; provisional score.",
                recommend_advance=False,
            )

        avg_rating = sum(f.rating for f in feedback_summaries) / len(feedback_summaries)
        # Ratings are 1-5 -> map to 0-100.
        rating_pct = (avg_rating - 1) / 4 * 100
        recommend_ratio = sum(1 for f in feedback_summaries if f.recommend) / len(
            feedback_summaries
        )
        # Blend average rating (70%) with recommend consensus (30%).
        pct = round(0.7 * rating_pct + 0.3 * recommend_ratio * 100)

        human = [f for f in feedback_summaries if f.source == "human"]
        bot = [f for f in feedback_summaries if f.source == "bot"]
        rationale = (
            f"Average rating {avg_rating:.1f}/5 across {len(feedback_summaries)} "
            f"review(s) ({len(human)} human, {len(bot)} bot). "
            f"{round(recommend_ratio * 100)}% recommend advancing."
        )
        # Advance when the majority recommend and the blended score is solid.
        recommend_advance = recommend_ratio >= 0.5 and pct >= 60
        return InterviewJudgment(
            pct=pct, rationale=rationale, recommend_advance=recommend_advance
        )


class _LLMAssessmentProvider(AssessmentProvider):
    """Shared base for real LLM providers.

    Subclasses implement `_complete(prompt)` returning model text; we parse a
    leading integer percentage and use the remainder as the rationale. Kept
    simple on purpose — a production version would use structured/JSON output.
    """

    def _complete(self, prompt: str) -> str:  # pragma: no cover - needs live API
        raise NotImplementedError

    @staticmethod
    def _parse_pct(text: str, default: int = 50) -> int:
        m = re.search(r"\b(\d{1,3})\b", text)
        if not m:
            return default
        return max(0, min(100, int(m.group(1))))

    def score_resume(self, resume_text: str, requirements: str) -> ResumeScore:  # pragma: no cover
        prompt = (
            "Score 0-100 how well this resume matches the job requirements. "
            "Reply with the number, then a one-line rationale.\n\n"
            f"Requirements:\n{requirements}\n\nResume:\n{resume_text}"
        )
        out = self._complete(prompt)
        return ResumeScore(pct=self._parse_pct(out), rationale=out.strip())

    def judge_interview(  # pragma: no cover
        self, transcript: str, feedback_summaries: list[FeedbackInput]
    ) -> InterviewJudgment:
        fb = "\n".join(
            f"- [{f.source}] rating {f.rating}/5, "
            f"recommend={f.recommend}: {f.notes}"
            for f in feedback_summaries
        )
        prompt = (
            "Given the interview transcript and reviewer feedback, score 0-100 "
            "the candidate's suitability, then a one-line rationale.\n\n"
            f"Transcript:\n{transcript}\n\nFeedback:\n{fb}"
        )
        out = self._complete(prompt)
        pct = self._parse_pct(out)
        return InterviewJudgment(
            pct=pct, rationale=out.strip(), recommend_advance=pct >= 60
        )


def get_assessment_provider() -> AssessmentProvider:
    """Select provider from settings; fall back to mock on missing key.

    Real LLM providers are only constructed when their API key is present.
    Otherwise we log a warning and use the deterministic mock so the workflow
    always runs.
    """
    provider = getattr(settings, "assessment_provider", "mock")

    if provider == "openai" and settings.openai_api_key:
        return _OpenAIAssessmentProvider(settings.openai_api_key, settings.openai_model)
    if provider == "gemini" and settings.gemini_api_key:
        return _GeminiAssessmentProvider(settings.gemini_api_key, settings.gemini_model)

    if provider in ("openai", "gemini"):
        logger.warning(
            "ASSESSMENT_PROVIDER=%s but no API key set; using mock.", provider
        )
    return MockAssessmentProvider()


class _OpenAIAssessmentProvider(_LLMAssessmentProvider):  # pragma: no cover
    name = "openai"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    def _complete(self, prompt: str) -> str:
        from openai import OpenAI

        client = OpenAI(api_key=self._api_key)
        resp = client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content or ""


class _GeminiAssessmentProvider(_LLMAssessmentProvider):  # pragma: no cover
    name = "gemini"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    def _complete(self, prompt: str) -> str:
        import google.generativeai as genai

        genai.configure(api_key=self._api_key)
        model = genai.GenerativeModel(self._model)
        return model.generate_content(prompt).text or ""
