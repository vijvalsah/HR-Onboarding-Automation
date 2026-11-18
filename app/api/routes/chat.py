"""Global assistant chat: SSE streaming backed by the pluggable chat service."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import get_token_payload
from app.services.chat import get_chat_provider

router = APIRouter()


class ChatStreamRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


async def _sse(message: str) -> AsyncIterator[bytes]:
    """Emit provider chunks as Server-Sent Events.

    Wire format (unchanged, the frontend depends on it):
        data: {"text": "<chunk>"}\n\n   (repeated)
        data: {"done": true}\n\n        (terminator)
    """
    provider = get_chat_provider()
    async for chunk in provider.stream(message):
        payload = json.dumps({"text": chunk}, ensure_ascii=False)
        yield f"data: {payload}\n\n".encode("utf-8")
    yield f"data: {json.dumps({'done': True})}\n\n".encode("utf-8")


@router.post("/chat/stream")
async def chat_stream(
    body: ChatStreamRequest,
    _payload: dict = Depends(get_token_payload),
) -> StreamingResponse:
    return StreamingResponse(
        _sse(body.message),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
