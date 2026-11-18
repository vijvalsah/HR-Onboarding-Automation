"""Pluggable chat service.

Routes talk to a single `ChatProvider` interface and never import an LLM SDK
directly. The concrete provider is chosen from settings at call time:

    CHAT_PROVIDER=mock    -> offline canned streaming (default, no key needed)
    CHAT_PROVIDER=openai  -> OpenAI, requires OPENAI_API_KEY + `openai` package
    CHAT_PROVIDER=gemini  -> Gemini, requires GEMINI_API_KEY + `google-generativeai`

The SDKs are imported lazily inside their providers so the default install stays
lean and the app runs with zero external dependencies in mock mode.

Every provider exposes the same async streaming contract: `stream(message)`
yields text chunks. `complete(message)` collects them into one string for the
non-streaming endpoint. This keeps the SSE wire format identical across
providers, so the frontend never has to change.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

SYSTEM_PROMPT = (
    "You are an onboarding assistant for new hires and candidates. Answer "
    "questions about interviews, offers, first-day logistics, and HR processes "
    "concisely and warmly. If you are unsure, suggest contacting the recruiter."
)


class ChatProvider(ABC):
    """Common interface for all chat backends."""

    name: str = "base"

    @abstractmethod
    def stream(self, message: str) -> AsyncIterator[str]:
        """Yield the reply as a sequence of text chunks."""
        raise NotImplementedError

    async def complete(self, message: str) -> str:
        """Collect the full reply as a single string."""
        parts: list[str] = []
        async for chunk in self.stream(message):
            parts.append(chunk)
        return "".join(parts)


class MockChatProvider(ChatProvider):
    """Offline provider: deterministic canned reply, streamed in chunks.

    No network, no key, no cost. Used by default and in tests.
    """

    name = "mock"

    def __init__(self, *, chunk_size: int = 28, delay: float = 0.03) -> None:
        self._chunk_size = chunk_size
        self._delay = delay

    async def stream(self, message: str) -> AsyncIterator[str]:
        reply = (
            "Here's a helpful reply from the onboarding assistant (mock mode — "
            "no live model configured). "
            f"You asked: \u201c{message.strip()[:200]}\u201d. "
            "Set CHAT_PROVIDER and an API key to use a real model behind this "
            "same interface."
        )
        for i in range(0, len(reply), self._chunk_size):
            yield reply[i : i + self._chunk_size]
            if self._delay:
                await asyncio.sleep(self._delay)


class OpenAIChatProvider(ChatProvider):
    """OpenAI provider. Lazily imports the `openai` SDK."""

    name = "openai"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    async def stream(self, message: str) -> AsyncIterator[str]:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise RuntimeError(
                "CHAT_PROVIDER=openai requires the 'openai' package. "
                "Install it with: pip install openai"
            ) from exc

        client = AsyncOpenAI(api_key=self._api_key)
        stream = await client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ],
            stream=True,
        )
        async for event in stream:
            delta = event.choices[0].delta.content
            if delta:
                yield delta


class GeminiChatProvider(ChatProvider):
    """Google Gemini provider. Lazily imports `google-generativeai`."""

    name = "gemini"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    async def stream(self, message: str) -> AsyncIterator[str]:
        try:
            import google.generativeai as genai
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise RuntimeError(
                "CHAT_PROVIDER=gemini requires the 'google-generativeai' package. "
                "Install it with: pip install google-generativeai"
            ) from exc

        genai.configure(api_key=self._api_key)
        model = genai.GenerativeModel(
            self._model, system_instruction=SYSTEM_PROMPT
        )
        # The SDK's streaming call is blocking; run it off the event loop.
        response = await asyncio.to_thread(
            model.generate_content, message, stream=True
        )
        for chunk in response:
            text = getattr(chunk, "text", None)
            if text:
                yield text


def get_chat_provider() -> ChatProvider:
    """Select the provider from settings, falling back to mock when a real
    provider is requested but its API key is missing (so the app never crashes
    on a misconfiguration — it degrades to the offline reply and logs a warning).
    """
    provider = settings.chat_provider

    if provider == "openai":
        if settings.openai_api_key:
            return OpenAIChatProvider(settings.openai_api_key, settings.openai_model)
        logger.warning("CHAT_PROVIDER=openai but OPENAI_API_KEY is unset; using mock.")
        return MockChatProvider()

    if provider == "gemini":
        if settings.gemini_api_key:
            return GeminiChatProvider(settings.gemini_api_key, settings.gemini_model)
        logger.warning("CHAT_PROVIDER=gemini but GEMINI_API_KEY is unset; using mock.")
        return MockChatProvider()

    return MockChatProvider()
