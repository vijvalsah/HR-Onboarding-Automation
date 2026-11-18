"""Tests for the mock external-integration services."""

from app.services.integrations import (
    MockCallBotService,
    MockEmailService,
    MockTeamsService,
    MockTranscriptionService,
)


def test_email_records_outbox() -> None:
    svc = MockEmailService()
    assert svc.outbox == []
    msg = svc.send(to="a@example.com", subject="Hi", body="Body")
    assert len(svc.outbox) == 1
    assert svc.outbox[0].to == "a@example.com"
    assert msg.subject == "Hi"
    assert msg.sent_at is not None


def test_teams_link_is_unique_and_plausible() -> None:
    svc = MockTeamsService()
    a = svc.create_meeting(subject="Round 1")
    b = svc.create_meeting(subject="Round 1")
    assert a.startswith("https://teams.microsoft.com/")
    assert a != b  # each meeting gets its own id


def test_call_bot_confirms_availability() -> None:
    svc = MockCallBotService()
    result = svc.call_for_availability(name="Morgan Patel", email="m@example.com")
    assert result.confirmed is True
    assert "Morgan Patel" in result.notes


def test_transcription_produces_transcript() -> None:
    svc = MockTranscriptionService()
    text = svc.transcribe(candidate_name="Morgan Patel", round_name="Technical")
    assert "Morgan Patel" in text
    assert "Technical" in text
    assert len(text) > 100
