"""Chat tests: SSE wire format + provider abstraction.

The frontend depends on the exact SSE frames (data: {"text": ...} then
data: {"done": true}), so we assert on that contract, and on the mock provider
degradation behavior.
"""

import json

import httpx
import pytest

from app.services.chat import (
    MockChatProvider,
    get_chat_provider,
)
from tests.conftest import auth_header, login


def _parse_sse(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: ") :]))
    return events


async def test_chat_stream_sse_format(client: httpx.AsyncClient) -> None:
    token = await login(client, "candidate@example.com")
    resp = await client.post(
        "/api/v1/chat/stream",
        headers=auth_header(token),
        json={"message": "What is my offer timeline?"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(resp.text)
    # At least one text chunk, and the final frame is the done terminator.
    assert any("text" in e for e in events)
    assert events[-1] == {"done": True}

    # Reassembled text echoes the question (mock behavior).
    reassembled = "".join(e["text"] for e in events if "text" in e)
    assert "offer timeline" in reassembled


async def test_chat_stream_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/chat/stream", json={"message": "hi"})
    assert resp.status_code in (401, 403)


async def test_candidate_chat_uses_service(client: httpx.AsyncClient) -> None:
    token = await login(client, "candidate@example.com")
    resp = await client.post(
        "/api/v1/candidate/chat",
        headers=auth_header(token),
        json={"message": "When do I start?"},
    )
    assert resp.status_code == 200
    reply = resp.json()["reply"]
    assert "When do I start?" in reply


async def test_default_provider_is_mock() -> None:
    provider = get_chat_provider()
    assert isinstance(provider, MockChatProvider)


async def test_mock_provider_complete_matches_stream() -> None:
    p = MockChatProvider(delay=0.0)
    streamed = "".join([chunk async for chunk in p.stream("hello")])
    completed = await p.complete("hello")
    assert streamed == completed
    assert "hello" in completed


def test_missing_message_rejected() -> None:
    # Empty message violates the min_length=1 constraint -> handled by pydantic.
    with pytest.raises(Exception):
        from app.api.routes.chat import ChatStreamRequest

        ChatStreamRequest(message="")
