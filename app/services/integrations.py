"""External integration services (email, Teams meetings, call bot, transcription).

These are the seams where the app touches the outside world. Each is defined as
an interface with a fully-functional MOCK implementation so the entire hiring
workflow runs and is testable locally with zero external credentials.

Swapping in a real provider is a targeted change, not a rewrite:
  - EmailService        -> SMTP / SendGrid / SES
  - TeamsService        -> Microsoft Graph online-meetings API
  - CallBotService      -> Twilio Voice + a speech IVR / voice agent
  - TranscriptionService-> Whisper / Azure Speech / AssemblyAI on the recording

The mocks are deterministic and observable (EmailService records every message)
so tests can assert on behavior. Real implementations would keep these method
signatures and return shapes unchanged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.logging import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------
@dataclass
class SentEmail:
    to: str
    subject: str
    body: str
    sent_at: datetime


class EmailService:
    """Interface for sending email."""

    def send(self, *, to: str, subject: str, body: str) -> SentEmail:
        raise NotImplementedError


class MockEmailService(EmailService):
    """Records outbound email in memory instead of sending it.

    A module-level singleton (see `email_service`) keeps an outbox that tests
    and the UI can inspect. A real service would deliver via SMTP/provider and
    likely not retain bodies.
    """

    def __init__(self) -> None:
        self.outbox: list[SentEmail] = []

    def send(self, *, to: str, subject: str, body: str) -> SentEmail:
        msg = SentEmail(to=to, subject=subject, body=body, sent_at=datetime.now(UTC))
        self.outbox.append(msg)
        logger.info("MockEmail -> %s | %s", to, subject)
        return msg


# --------------------------------------------------------------------------
# Teams meeting links
# --------------------------------------------------------------------------
class TeamsService:
    """Interface for creating meeting links."""

    def create_meeting(self, *, subject: str) -> str:
        raise NotImplementedError


class MockTeamsService(TeamsService):
    """Generates a plausible-looking Teams meeting URL (not a real meeting).

    A real implementation would call MS Graph `POST /me/onlineMeetings` and
    return the `joinWebUrl`.
    """

    def create_meeting(self, *, subject: str) -> str:
        meeting_id = uuid.uuid4().hex
        return f"https://teams.microsoft.com/l/meetup-join/mock/{meeting_id}"


# --------------------------------------------------------------------------
# Call bot (availability screening)
# --------------------------------------------------------------------------
@dataclass
class CallResult:
    confirmed: bool
    notes: str


class CallBotService:
    """Interface for the availability-screening phone bot."""

    def call_for_availability(self, *, name: str, email: str) -> CallResult:
        raise NotImplementedError


class MockCallBotService(CallBotService):
    """Simulates an availability call. Deterministic: always 'reaches' the
    candidate and records a confirmation note.

    A real implementation would place a Twilio call, run an IVR / voice agent
    to ask about availability and interest, and interpret the response.
    """

    def call_for_availability(self, *, name: str, email: str) -> CallResult:
        notes = (
            f"Automated availability call to {name}. Candidate confirmed interest "
            "and general availability for interviews over the next two weeks. "
            "(Simulated call — no real telephony configured.)"
        )
        logger.info("MockCallBot -> %s availability confirmed", email)
        return CallResult(confirmed=True, notes=notes)


# --------------------------------------------------------------------------
# Transcription
# --------------------------------------------------------------------------
class TranscriptionService:
    """Interface for producing an interview transcript."""

    def transcribe(self, *, candidate_name: str, round_name: str) -> str:
        raise NotImplementedError


class MockTranscriptionService(TranscriptionService):
    """Produces a deterministic sample transcript for a round.

    A real implementation would transcribe the recorded audio (Whisper / Azure
    Speech) captured by a meeting bot that joined the Teams call.
    """

    def transcribe(self, *, candidate_name: str, round_name: str) -> str:
        return (
            f"[Transcript — {round_name}]\n"
            f"Interviewer: Thanks for joining, {candidate_name}. "
            "Let's start with your background.\n"
            f"{candidate_name}: Happy to be here. I've worked on backend "
            "services, APIs, and databases, and enjoy system design problems.\n"
            "Interviewer: Walk me through a system you designed recently.\n"
            f"{candidate_name}: I designed a service handling high write "
            "throughput with a queue, idempotent consumers, and a relational "
            "store with read replicas.\n"
            "Interviewer: Good. How do you approach testing?\n"
            f"{candidate_name}: Unit tests for logic, integration tests for the "
            "API surface, and CI gates before merge.\n"
            "Interviewer: Great, thank you.\n"
            "(Auto-generated transcript — no real recording was processed.)"
        )


# --------------------------------------------------------------------------
# Singletons (swap these for real implementations when credentials exist)
# --------------------------------------------------------------------------
email_service: EmailService = MockEmailService()
teams_service: TeamsService = MockTeamsService()
call_bot_service: CallBotService = MockCallBotService()
transcription_service: TranscriptionService = MockTranscriptionService()
