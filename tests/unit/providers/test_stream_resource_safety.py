"""Stream resource safety — streams MUST be closed on ALL exit paths.

Covers: normal completion, CancelledError (watchdog), exception during iteration.
Ensures subsequent calls after cancellation still work correctly.
"""
from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from framework.core.constants import FinishReason
from framework.core.llm_struct import (
    LLMTimeoutPolicy,
    RuntimeSafetyPolicy,
    TurnTimeoutPolicy,
)


# ── Helpers ──────────────────────────────────────────────────────────────


class TrackableStream:
    """Mimics openai.AsyncStream — async iterator with close()."""

    def __init__(self, chunks, *, cancel_at=None):
        self._chunks = list(chunks)
        self._idx = 0
        self._cancel_at = cancel_at
        self.close_called = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._cancel_at is not None and self._idx >= self._cancel_at:
            raise asyncio.CancelledError("watchdog")
        if self._idx >= len(self._chunks):
            raise StopAsyncIteration
        c = self._chunks[self._idx]
        self._idx += 1
        return c

    async def close(self):
        self.close_called = True


def _openai_chunk(content=None, finish_reason=None):
    delta = MagicMock()
    delta.content = content
    delta.tool_calls = None
    delta.model_extra = None
    choice = MagicMock()
    choice.delta = delta
    choice.finish_reason = finish_reason
    chunk = MagicMock()
    chunk.choices = [choice]
    chunk.usage = None
    return chunk


class _LiteDelta:
    __slots__ = ("content", "tool_calls", "model_extra")

    def __init__(self, content=None):
        self.content = content
        self.tool_calls = None
        self.model_extra = {}


class _LiteChoice:
    __slots__ = ("delta", "finish_reason")

    def __init__(self, content, finish_reason=None):
        self.delta = _LiteDelta(content)
        self.finish_reason = finish_reason


class _LiteChunk:
    __slots__ = ("choices",)

    def __init__(self, content, finish_reason=None):
        self.choices = [_LiteChoice(content, finish_reason)]


def _litellm_chunk(content=None, finish_reason=None):
    return _LiteChunk(content, finish_reason)


class TrackableLiteLLMResponse:
    """Mimics litellm async response — async iterator with aclose()."""

    def __init__(self, chunks, *, cancel_at=None):
        self._chunks = list(chunks)
        self._idx = 0
        self._cancel_at = cancel_at
        self.aclose_called = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._cancel_at is not None and self._idx >= self._cancel_at:
            raise asyncio.CancelledError("watchdog")
        if self._idx >= len(self._chunks):
            raise StopAsyncIteration
        c = self._chunks[self._idx]
        self._idx += 1
        return c

    async def aclose(self):
        self.aclose_called = True


# ── Minimal baseline: verify Python async finally behavior ───────────────


class TestAsyncFinallyBaseline:
    """Verify that Python's try/finally runs aclose() on CancelledError."""

    @pytest.mark.asyncio
    async def test_finally_no_wait_for(self):
        """Baseline: aclose works without asyncio.wait_for."""
        closed = False

        class It:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise asyncio.CancelledError("x")

            async def aclose(self):
                nonlocal closed
                closed = True

        it = It()
        try:
            try:
                while True:
                    try:
                        await anext(it)
                    except StopAsyncIteration:
                        break
            finally:
                with contextlib.suppress(BaseException):
                    close = getattr(it, "aclose", None)
                    if close is not None:
                        await close()
        except asyncio.CancelledError:
            pass

        assert closed, "aclose() must be called (without wait_for)"

    @pytest.mark.asyncio
    async def test_finally_with_wait_for(self):
        """Does aclose work when CancelledError goes through asyncio.wait_for?"""
        closed = False

        class It:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise asyncio.CancelledError("x")

            async def aclose(self):
                nonlocal closed
                closed = True

        it = It()
        try:
            try:
                while True:
                    try:
                        await asyncio.wait_for(anext(it), timeout=5)
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        break
            finally:
                with contextlib.suppress(BaseException):
                    close = getattr(it, "aclose", None)
                    if close is not None:
                        await close()
        except asyncio.CancelledError:
            pass

        assert closed, "aclose() must be called even through asyncio.wait_for"


# ── OpenAI Provider ──────────────────────────────────────────────────────


class TestOpenAIStreamResourceSafety:

    @pytest.fixture
    def provider(self):
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(request_timeout_seconds=10, stream_idle_timeout_seconds=30),
            turn=TurnTimeoutPolicy(),
        )
        with patch("framework.providers.openai_provider.AsyncOpenAI"):
            from framework.providers.openai_provider import OpenAIProvider
            p = OpenAIProvider(model="gpt-4o", api_key="sk-test", safety=safety)
            yield p

    @pytest.mark.asyncio
    async def test_stream_closed_on_normal_completion(self, provider):
        """stream.close() MUST be called after all chunks are consumed."""
        stream = TrackableStream([
            _openai_chunk(content="hello"),
            _openai_chunk(content=" world", finish_reason="stop"),
        ])
        provider._client.chat.completions.create = AsyncMock(return_value=stream)

        result = await provider.chat_stream(messages=[{"role": "user", "content": "hi"}])

        assert result.content == "hello world"
        assert stream.close_called, "stream.close() not called on normal completion"

    @pytest.mark.asyncio
    async def test_stream_closed_on_cancellation(self, provider):
        """stream.close() MUST be called when CancelledError interrupts iteration."""
        stream = TrackableStream([], cancel_at=0)
        provider._client.chat.completions.create = AsyncMock(return_value=stream)

        with pytest.raises(asyncio.CancelledError):
            await provider.chat_stream(messages=[{"role": "user", "content": "hi"}])

        assert stream.close_called, "stream.close() not called on CancelledError"

    @pytest.mark.asyncio
    async def test_next_call_works_after_cancellation(self, provider):
        """After a cancelled stream, subsequent chat_stream must succeed."""
        # First call: cancelled
        bad = TrackableStream([], cancel_at=0)
        provider._client.chat.completions.create = AsyncMock(return_value=bad)
        with pytest.raises(asyncio.CancelledError):
            await provider.chat_stream(messages=[{"role": "user", "content": "1"}])
        assert bad.close_called

        # Second call: normal
        ok = TrackableStream([
            _openai_chunk(content="recovered", finish_reason="stop"),
        ])
        provider._client.chat.completions.create = AsyncMock(return_value=ok)
        result = await provider.chat_stream(messages=[{"role": "user", "content": "2"}])

        assert result.content == "recovered"
        assert result.finish_reason == "stop"
        assert ok.close_called


# ── LiteLLM Provider ─────────────────────────────────────────────────────


class TestLiteLLMStreamResourceSafety:

    @pytest.fixture
    def provider(self):
        with patch.dict("os.environ", {"LITELLM_LOG": "ERROR"}):
            from framework.providers.litellm_provider import LiteLLMProvider
            p = LiteLLMProvider(model="gpt-4", api_key="test-key")
            p._acompletion = AsyncMock()
            return p

    @pytest.mark.asyncio
    async def test_stream_closed_on_normal_completion(self, provider):
        """iterator.aclose() MUST be called after all chunks are consumed."""
        resp = TrackableLiteLLMResponse([
            _litellm_chunk(content="hello"),
            _litellm_chunk(content=" world", finish_reason="stop"),
        ])
        provider._acompletion = AsyncMock(return_value=resp)

        result = await provider.chat_stream(messages=[{"role": "user", "content": "hi"}])

        assert result.content == "hello world"
        assert resp.aclose_called, "aclose() not called on normal completion"

    @pytest.mark.asyncio
    async def test_stream_closed_on_cancellation(self, provider):
        """iterator.aclose() MUST be called when CancelledError interrupts iteration."""
        resp = TrackableLiteLLMResponse([], cancel_at=0)
        provider._acompletion = AsyncMock(return_value=resp)

        with pytest.raises(asyncio.CancelledError):
            await provider.chat_stream(messages=[{"role": "user", "content": "hi"}])

        assert resp.aclose_called, "aclose() not called on CancelledError"

    @pytest.mark.asyncio
    async def test_next_call_works_after_cancellation(self, provider):
        """After a cancelled stream, subsequent chat_stream must succeed."""
        bad = TrackableLiteLLMResponse([], cancel_at=0)
        provider._acompletion = AsyncMock(return_value=bad)
        with pytest.raises(asyncio.CancelledError):
            await provider.chat_stream(messages=[{"role": "user", "content": "1"}])
        assert bad.aclose_called

        ok = TrackableLiteLLMResponse([
            _litellm_chunk(content="recovered", finish_reason="stop"),
        ])
        provider._acompletion = AsyncMock(return_value=ok)
        result = await provider.chat_stream(messages=[{"role": "user", "content": "2"}])

        assert result.content == "recovered"
        assert result.finish_reason == "stop"
        assert ok.aclose_called
