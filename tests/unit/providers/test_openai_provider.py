"""Tests for modex_agent.providers.openai_provider."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("openai")  # skip if openai not installed (CI [dev] doesn't include [llm] deps)

from modex_agent.agents.react.message_builder import build_assistant_message
from modex_agent.core.constants import FinishReason, ReasoningEffort
from modex_agent.core.llm_struct import (
    LLMErrorKind,
    LLMTimeoutPolicy,
    RuntimeSafetyPolicy,
    TurnTimeoutPolicy,
)
from modex_agent.core.message import ChatMessage, ContentFormat
from modex_agent.core.types import LLMResponse, MessageRole, ToolCall
from modex_agent.providers.openai_provider import OpenAIProvider
from modex_agent.providers.shared.constants import PROMPT_CACHE_KEY_PARAM, REASONING_EFFORT_PARAM


class TestOpenAIProviderChat:
    """Unit tests for OpenAIProvider.chat().

    chat() now delegates to chat_stream_with_retry internally (stream=True)
    for prompt cache benefits, while returning a complete LLMResponse to the caller.
    """

    @pytest.fixture
    def provider(self):
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(request_timeout_seconds=10, stream_idle_timeout_seconds=30),
            turn=TurnTimeoutPolicy(),
        )
        with patch("modex_agent.providers.openai_provider.AsyncOpenAI") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            p = OpenAIProvider(model="gpt-4o", api_key="sk-test", safety=safety)
            p._client = mock_client
            yield p

    def _make_chunk(self, content=None, finish_reason=None, reasoning=None, usage=None):
        """Build a mock ChatCompletionChunk."""
        delta = MagicMock()
        delta.content = content
        delta.tool_calls = None
        delta.model_extra = {"reasoning_content": reasoning} if reasoning else None

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = finish_reason

        chunk = MagicMock()
        chunk.choices = [choice]
        chunk.usage = usage
        return chunk

    async def _stream_chunks(self, chunks):
        for c in chunks:
            yield c

    @pytest.mark.asyncio
    async def test_chat_returns_content(self, provider):
        chunks = [
            self._make_chunk(content="Hello"),
            self._make_chunk(content=", world!", finish_reason="stop",
                             usage=MagicMock(prompt_tokens=100, completion_tokens=50, total_tokens=150)),
        ]
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks(chunks)
        )

        result = await provider.chat(messages=[ChatMessage(role=MessageRole.USER, content="hi")])

        assert isinstance(result, LLMResponse)
        assert result.content == "Hello, world!"
        assert result.finish_reason == "stop"
        assert result.usage == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
                                 "cache_read_input_tokens": 1, "reasoning_tokens": 1}

    @pytest.mark.asyncio
    async def test_chat_extracts_cache_from_final_usage_chunk(self, provider):
        """The final chunk (empty choices) carries correct cache tokens.

        Some providers (e.g. stepfun) send usage on EVERY chunk with
        cached_tokens=0, then the final chunk (choices=[]) has the
        correct cached_tokens value. The provider must not skip it.
        """
        # Chunks with choices + usage (cached=0, like stepfun)
        content_chunks = [
            self._make_chunk(content="hi", usage=MagicMock(
                prompt_tokens=1000, completion_tokens=50, total_tokens=1050,
                prompt_tokens_details=MagicMock(cached_tokens=0),
                completion_tokens_details=MagicMock(reasoning_tokens=0),
            )),
            self._make_chunk(content=" there", finish_reason="stop", usage=MagicMock(
                prompt_tokens=1000, completion_tokens=50, total_tokens=1050,
                prompt_tokens_details=MagicMock(cached_tokens=0),
                completion_tokens_details=MagicMock(reasoning_tokens=0),
            )),
        ]
        # Final chunk: NO choices, but usage with correct cache tokens
        final_chunk = MagicMock()
        final_chunk.choices = []
        final_chunk.usage = MagicMock(
            prompt_tokens=1000, completion_tokens=50, total_tokens=1050,
            prompt_tokens_details=MagicMock(cached_tokens=800),
            completion_tokens_details=MagicMock(reasoning_tokens=30),
        )

        all_chunks = content_chunks + [final_chunk]
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks(all_chunks)
        )

        result = await provider.chat(messages=[ChatMessage(role=MessageRole.USER, content="hi")])

        assert result.usage.get("cache_read_input_tokens") == 800, (
            f"Expected 800, got {result.usage.get('cache_read_input_tokens')} — "
            "final usage chunk with empty choices was skipped"
        )
        assert result.usage.get("reasoning_tokens") == 30

    @pytest.mark.asyncio
    async def test_chat_with_tool_calls(self, provider):
        func = MagicMock()
        func.name = "search"
        func.arguments = '{"query": "test"}'

        tc = MagicMock()
        tc.index = 0
        tc.id = "c1"
        tc.function = func

        delta = MagicMock()
        delta.content = None
        delta.tool_calls = [tc]
        delta.model_extra = None

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = "tool_calls"

        chunk = MagicMock()
        chunk.choices = [choice]
        chunk.usage = None

        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks([chunk])
        )

        result = await provider.chat(messages=[ChatMessage(role=MessageRole.USER, content="search")])

        assert result.has_tool_calls
        assert result.tool_calls[0].tool_name == "search"
        assert result.tool_calls[0].arguments == {"query": "test"}
        assert result.finish_reason == "tool_calls"

    @pytest.mark.asyncio
    async def test_chat_with_reasoning(self, provider):
        chunks = [
            self._make_chunk(reasoning="step by step..."),
            self._make_chunk(content="answer", finish_reason="stop"),
        ]
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks(chunks)
        )

        result = await provider.chat(messages=[ChatMessage(role=MessageRole.USER, content="?")])
        assert result.reasoning_content == "step by step..."

    @pytest.mark.asyncio
    async def test_chat_error_returns_error_response(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            side_effect=Exception("connection refused")
        )

        result = await provider.chat(messages=[ChatMessage(role=MessageRole.USER, content="hi")])

        assert result.finish_reason == FinishReason.ERROR.value
        assert result.error_info is not None
        assert result.error_info.kind == LLMErrorKind.CONNECTION

    @pytest.mark.asyncio
    async def test_chat_passes_parameters_correctly(self, provider):
        chunks = [self._make_chunk(content="ok", finish_reason="stop")]
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks(chunks)
        )

        await provider.chat(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            model="gpt-4o-mini",
            temperature=0.3,
            max_output_tokens=500,
            tools=[{"type": "function", "function": {"name": "t1", "parameters": {}}}],
        )

        call_kwargs = provider._client.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == "gpt-4o-mini"
        assert call_kwargs["temperature"] == 0.3
        assert call_kwargs["top_p"] == 0.95
        assert call_kwargs["max_tokens"] == 500
        assert len(call_kwargs["tools"]) == 1
        # chat() now uses streaming internally for cache benefits
        assert call_kwargs["stream"] is True


class TestOpenAIProviderReasoningEffort:
    """OpenAIProvider reasoning_effort parameter tests."""

    def _make_chunk(self, content=None, finish_reason=None, reasoning=None, usage=None):
        """Build a mock ChatCompletionChunk."""
        delta = MagicMock()
        delta.content = content
        delta.tool_calls = None
        delta.model_extra = {"reasoning_content": reasoning} if reasoning else None

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = finish_reason

        chunk = MagicMock()
        chunk.choices = [choice]
        chunk.usage = usage
        return chunk

    async def _stream_chunks(self, chunks):
        for c in chunks:
            yield c

    @pytest.fixture
    def provider(self):
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(request_timeout_seconds=10, stream_idle_timeout_seconds=30),
            turn=TurnTimeoutPolicy(),
        )
        with patch("modex_agent.providers.openai_provider.AsyncOpenAI") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            p = OpenAIProvider(
                model="gpt-4o",
                api_key="sk-test",
                reasoning_effort=ReasoningEffort.MEDIUM,
                safety=safety,
            )
            p._client = mock_client
            yield p

    @pytest.mark.asyncio
    async def test_reasoning_effort_passed_when_non_none(self, provider):
        chunks = [self._make_chunk(content="ok", finish_reason="stop")]
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks(chunks)
        )
        await provider.chat(messages=[ChatMessage(role=MessageRole.USER, content="hi")])
        assert provider._client.chat.completions.create.call_args.kwargs[REASONING_EFFORT_PARAM] == ReasoningEffort.MEDIUM.value

    def test_reasoning_effort_omitted_when_none(self):
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(request_timeout_seconds=10, stream_idle_timeout_seconds=30),
            turn=TurnTimeoutPolicy(),
        )
        with patch("modex_agent.providers.openai_provider.AsyncOpenAI") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            p = OpenAIProvider(
                model="gpt-4o",
                api_key="sk-test",
                reasoning_effort=ReasoningEffort.NONE,
                safety=safety,
            )
            p._client = mock_client
            params = p._build_params(messages=[ChatMessage(role=MessageRole.USER, content="hi")])
            assert REASONING_EFFORT_PARAM not in params


class TestBuildParamsStripsGovernanceFields:
    """_build_params must strip governance-internal fields from messages.

    Fields like content_format, truncatable_paths, metadata, meta_context_lossy
    are internal governance metadata that must never reach external APIs.
    """

    @pytest.fixture
    def provider(self):
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(request_timeout_seconds=10, stream_idle_timeout_seconds=30),
            turn=TurnTimeoutPolicy(),
        )
        with patch("modex_agent.providers.openai_provider.AsyncOpenAI") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            p = OpenAIProvider(model="gpt-4o", api_key="sk-test", safety=safety)
            p._client = mock_client
            yield p

    def test_strips_content_format_and_truncatable_paths(self, provider):
        messages = [
            ChatMessage(
                role=MessageRole.SYSTEM,
                content="sys",
                content_format=ContentFormat.XML,
                truncatable_paths=["content"],
            ),
            ChatMessage(role=MessageRole.USER, content="hi"),
        ]
        params = provider._build_params(messages=messages)
        api_msgs = params["messages"]

        assert "content_format" not in api_msgs[0]
        assert "truncatable_paths" not in api_msgs[0]
        assert api_msgs[0]["role"] == "system"
        assert api_msgs[0]["content"] == "sys"

    def test_strips_metadata_and_lossy_fields(self, provider):
        # Extras (metadata / meta_* governance fields) ride on ChatMessage
        # via extra="allow"; _build_params must strip them before the API call.
        messages = [
            ChatMessage.from_dict(
                {
                    "role": "assistant",
                    "content": "reply",
                    "metadata": {"source": "urb"},
                    "meta_context_lossy": True,
                    "meta_original_chars": 5000,
                    "meta_context_reduction": "content_truncated",
                }
            ),
        ]
        params = provider._build_params(messages=messages)
        api_msgs = params["messages"]

        assert "metadata" not in api_msgs[0]
        assert "meta_context_lossy" not in api_msgs[0]
        assert "meta_original_chars" not in api_msgs[0]
        assert "meta_context_reduction" not in api_msgs[0]
        assert api_msgs[0]["role"] == "assistant"
        assert api_msgs[0]["content"] == "reply"

    def test_preserves_standard_api_fields(self, provider):
        messages = [
            ChatMessage(role=MessageRole.USER, content="hi", name="user1"),
            ChatMessage(
                role=MessageRole.ASSISTANT,
                content=None,
                tool_calls=[ToolCall(tool_name="t", arguments={}, call_id="c1")],
            ),
            ChatMessage(role=MessageRole.TOOL, tool_call_id="c1", content="result"),
        ]
        params = provider._build_params(messages=messages)
        api_msgs = params["messages"]

        assert api_msgs[0] == {"role": "user", "content": "hi", "name": "user1"}
        assert api_msgs[1]["tool_calls"] is not None
        assert api_msgs[1]["tool_calls"][0] == {
            "id": "c1",
            "type": "function",
            "function": {"name": "t", "arguments": "{}"},
        }
        assert api_msgs[2]["tool_call_id"] == "c1"


class TestReasoningContentPassback:
    """DeepSeek thinking-mode passback: assistant tool-call turns replay reasoning_content.

    ``reasoning_content`` is stored on ChatMessage as a pydantic extra and
    persisted through ``to_dict()``/``from_dicts()`` (it must survive
    compaction / process restarts); ``_sanitize_api_messages`` must replay it
    ONLY on assistant messages carrying ``tool_calls`` — the API ignores it
    on tool-call-free turns, so dropping it there saves tokens.
    """

    @pytest.fixture
    def provider(self):
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(request_timeout_seconds=10, stream_idle_timeout_seconds=30),
            turn=TurnTimeoutPolicy(),
        )
        with patch("modex_agent.providers.openai_provider.AsyncOpenAI") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            p = OpenAIProvider(model="deepseek-reasoner", api_key="sk-test", safety=safety)
            p._client = mock_client
            yield p

    def _tool_call(self):
        return ToolCall(tool_name="search", arguments={"q": "x"}, call_id="c1")

    def _make_chunk(self, content=None, finish_reason=None):
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

    async def _stream_chunks(self, chunks):
        for c in chunks:
            yield c

    def test_reasoning_replayed_on_tool_call_turn(self, provider):
        messages = [
            build_assistant_message(None, [self._tool_call()], reasoning_content="thinking..."),
        ]
        params = provider._build_params(messages=messages)
        assert params["messages"][0]["reasoning_content"] == "thinking..."

    def test_reasoning_dropped_on_tool_call_free_turn(self, provider):
        messages = [
            build_assistant_message("answer", [], reasoning_content="thinking..."),
        ]
        params = provider._build_params(messages=messages)
        assert "reasoning_content" not in params["messages"][0]

    def test_no_reasoning_extra_leaves_payload_unchanged(self, provider):
        messages = [
            build_assistant_message(None, [self._tool_call()]),
        ]
        params = provider._build_params(messages=messages)
        assert "reasoning_content" not in params["messages"][0]
        assert params["messages"][0]["tool_calls"][0]["function"]["name"] == "search"

    def test_persistence_round_trip_replays_reasoning_on_wire(self, provider):
        """THE end-to-end persistence passback chain: build → to_dict →
        from_dicts (storage round-trip) → _sanitize_api_messages replays
        reasoning on the wire. This is the exact chain that previously
        starved after compaction / process restarts."""
        original = build_assistant_message(None, [self._tool_call()], reasoning_content="cot")
        rehydrated = ChatMessage.from_dicts([original.to_dict()])
        tool_msg = ChatMessage(
            role=MessageRole.TOOL, tool_call_id="c1", name="search", content="result"
        )
        api_msgs = provider._sanitize_api_messages([*rehydrated, tool_msg])
        assert api_msgs[0]["reasoning_content"] == "cot"
        assert api_msgs[0]["tool_calls"][0]["id"] == "c1"
        assert api_msgs[1]["role"] == "tool"
        assert api_msgs[1]["tool_call_id"] == "c1"

    def test_persistence_round_trip_plain_turn_omits_reasoning_on_wire(self, provider):
        """Plain assistant turn (no tool_calls) round-trips through storage,
        but the provider still omits reasoning on the wire (behavioral parity)."""
        original = build_assistant_message("answer", [], reasoning_content="cot")
        rehydrated = ChatMessage.from_dicts([original.to_dict()])
        api_msgs = provider._sanitize_api_messages(rehydrated)
        assert api_msgs[0]["content"] == "answer"
        assert "reasoning_content" not in api_msgs[0]

    @pytest.mark.asyncio
    async def test_react_replay_shape_end_to_end(self, provider):
        """[assistant(tool_calls+reasoning), tool(result)] history → the actual
        API request carries reasoning_content on the assistant message."""
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks([self._make_chunk(content="ok", finish_reason="stop")])
        )
        history = [
            build_assistant_message(
                None, [self._tool_call()], reasoning_content="chain of thought"
            ),
            ChatMessage(role=MessageRole.TOOL, tool_call_id="c1", name="search", content="result"),
        ]

        await provider.chat(messages=history)

        request_messages = provider._client.chat.completions.create.call_args.kwargs["messages"]
        assert request_messages[0]["reasoning_content"] == "chain of thought"
        assert request_messages[0]["tool_calls"][0]["id"] == "c1"
        assert request_messages[1]["role"] == "tool"
        assert request_messages[1]["tool_call_id"] == "c1"


class TestOpenAIProviderChatStream:
    """Unit tests for OpenAIProvider.chat_stream()."""

    @pytest.fixture
    def provider(self):
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(request_timeout_seconds=10, stream_idle_timeout_seconds=0.1),
            turn=TurnTimeoutPolicy(),
        )
        with patch("modex_agent.providers.openai_provider.AsyncOpenAI") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            p = OpenAIProvider(model="gpt-4o", api_key="sk-test", safety=safety)
            p._client = mock_client
            yield p

    def _make_chunk(self, content=None, finish_reason=None, reasoning=None, usage=None):
        """Build a mock ChatCompletionChunk."""
        delta = MagicMock()
        delta.content = content
        delta.tool_calls = None
        delta.model_extra = {"reasoning_content": reasoning} if reasoning else None

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = finish_reason

        chunk = MagicMock()
        chunk.choices = [choice]
        chunk.usage = usage
        return chunk

    async def _stream_chunks(self, chunks):
        for c in chunks:
            yield c

    @pytest.mark.asyncio
    async def test_chat_stream_content(self, provider):
        chunks = [
            self._make_chunk(content="Hello"),
            self._make_chunk(content=" world"),
            self._make_chunk(content="!", finish_reason="stop"),
        ]
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks(chunks)
        )

        deltas = []
        result = await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            on_content_delta=lambda d: deltas.append(d),
        )

        assert result.content == "Hello world!"
        assert result.finish_reason == "stop"
        assert deltas == ["Hello", " world", "!"]

    @pytest.mark.asyncio
    async def test_chat_stream_with_reasoning(self, provider):
        chunks = [
            self._make_chunk(reasoning="let me think..."),
            self._make_chunk(content="42", finish_reason="stop"),
        ]
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks(chunks)
        )

        reasoning_parts = []
        result = await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="?")],
            on_reasoning_delta=lambda d: reasoning_parts.append(d),
        )

        assert result.content == "42"
        assert result.reasoning_content == "let me think..."
        assert reasoning_parts == ["let me think..."]

    @pytest.mark.asyncio
    async def test_chat_stream_with_tool_calls(self, provider):
        func = MagicMock()
        func.name = "search"
        func.arguments = '{"query": "x"}'

        tc = MagicMock()
        tc.index = 0
        tc.id = "call_x"
        tc.function = func

        delta = MagicMock()
        delta.content = None
        delta.tool_calls = [tc]
        delta.model_extra = None

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = "tool_calls"

        chunk = MagicMock()
        chunk.choices = [choice]
        chunk.usage = None

        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks([chunk])
        )

        result = await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="search")],
        )

        assert result.has_tool_calls
        assert result.tool_calls[0].tool_name == "search"
        assert result.tool_calls[0].arguments == {"query": "x"}

    @pytest.mark.asyncio
    async def test_chat_stream_idle_timeout(self, provider):
        """When stream produces nothing, returns error response on idle timeout."""

        async def _slow_stream():
            await asyncio.sleep(999)
            if False:
                yield

        provider._client.chat.completions.create = AsyncMock(
            return_value=_slow_stream()
        )

        result = await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
        )

        assert result.finish_reason == FinishReason.ERROR.value
        assert result.error_info.kind == LLMErrorKind.TIMEOUT

    @pytest.mark.asyncio
    async def test_chat_stream_handles_empty_choices(self, provider):
        """Chunks with empty choices list should be skipped."""
        chunk_empty = MagicMock()
        chunk_empty.choices = []

        chunk_content = self._make_chunk(content="data", finish_reason="stop")

        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks([chunk_empty, chunk_content])
        )

        result = await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
        )

        assert result.content == "data"

    @pytest.mark.asyncio
    async def test_chat_stream_error(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            side_effect=Exception("connection refused")
        )

        result = await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
        )

        assert result.finish_reason == FinishReason.ERROR.value
        assert result.error_info.kind == LLMErrorKind.CONNECTION

    @pytest.mark.asyncio
    async def test_chat_stream_mid_stream_apierror_returns_error_response(self, provider):
        """A mid-stream APIError (e.g. GLM content moderation ``new_sensitive``)
        must be converted into a graceful error LLMResponse, not raised.

        Regression for the crash where the streaming iteration loop only caught
        StopAsyncIteration / TimeoutError, so any mid-stream exception escaped
        and aborted the whole agent turn.
        """
        from openai import APIError

        async def _stream_that_breaks_midway():
            yield self._make_chunk(content="partial answer")
            raise APIError(
                "output new_sensitive (1027)",
                request=MagicMock(),
                body=None,
            )

        provider._client.chat.completions.create = AsyncMock(
            return_value=_stream_that_breaks_midway()
        )

        deltas = []
        result = await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            on_content_delta=lambda d: deltas.append(d),
        )

        # Must not raise; must return a structured error response.
        assert result.finish_reason == FinishReason.ERROR.value
        assert result.error_info is not None
        # Content-moderation error is classified distinctly and non-retryable.
        assert result.error_info.kind == LLMErrorKind.CONTENT_FILTER
        assert result.error_info.should_retry is False
        # Partial content already streamed before the error must be preserved.
        assert deltas == ["partial answer"]
        assert "new_sensitive" in (result.error or "")


class TestLengthFinishDropsPendingToolCalls:
    """finish_reason=length (max_tokens ceiling) must drop pending tool calls.

    W0 audit P4: a stream cut at the token ceiling leaves tool calls truncated
    mid-arguments; repairing them into valid-looking (or empty) arguments
    produces calls the ReAct loop would execute unsafely. Only completed calls
    may ride the response when the stream ended at the length ceiling. Any
    other finish reason keeps the historical partial-flush behavior.
    """

    @pytest.fixture
    def provider(self):
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(request_timeout_seconds=10, stream_idle_timeout_seconds=30),
            turn=TurnTimeoutPolicy(),
        )
        with patch("modex_agent.providers.openai_provider.AsyncOpenAI") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            p = OpenAIProvider(model="gpt-4o", api_key="sk-test", safety=safety)
            p._client = mock_client
            yield p

    @staticmethod
    def _tool_call_delta(index, call_id, name, args):
        func = MagicMock()
        func.name = name
        func.arguments = args

        tc = MagicMock()
        tc.index = index
        tc.id = call_id
        tc.function = func
        return tc

    def _tool_chunk(self, tool_calls, finish_reason=None):
        delta = MagicMock()
        delta.content = None
        delta.tool_calls = tool_calls
        delta.model_extra = None

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = finish_reason

        chunk = MagicMock()
        chunk.choices = [choice]
        chunk.usage = None
        return chunk

    async def _stream_chunks(self, chunks):
        for c in chunks:
            yield c

    @pytest.mark.asyncio
    async def test_length_drop_keeps_only_completed_calls(self, provider):
        """Stream ends mid-arguments at the token ceiling → only the completed
        call rides the response; the truncated call is dropped."""
        chunks = [
            self._tool_chunk(
                [self._tool_call_delta(0, "call_1", "search", '{"query": "test"}')]
            ),
            # Truncated mid-arguments: unparseable, but repairable into a
            # valid-looking (wrong) payload — exactly the unsafe case.
            self._tool_chunk(
                [self._tool_call_delta(1, "call_2", "write_file", '{"path": "a.py", "content": "trunc')]
            ),
            self._tool_chunk([], finish_reason="length"),
        ]
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks(chunks)
        )

        result = await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="search")],
        )

        assert result.finish_reason == FinishReason.LENGTH.value
        assert [tc.tool_name for tc in result.tool_calls] == ["search"]
        assert result.tool_calls[0].arguments == {"query": "test"}

    @pytest.mark.asyncio
    async def test_length_drop_all_incomplete_leaves_tool_calls_empty(self, provider):
        """Every call truncated at the ceiling → tool_calls empty (the empty
        ending then flows into LengthGuard downstream)."""
        chunks = [
            self._tool_chunk(
                [self._tool_call_delta(0, "call_9", "write_file", '{"path": "a.py", "content": "trunc')]
            ),
            self._tool_chunk([], finish_reason="length"),
        ]
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks(chunks)
        )

        result = await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="write")],
        )

        assert result.finish_reason == FinishReason.LENGTH.value
        assert result.tool_calls == []
        assert result.has_tool_calls is False

    @pytest.mark.asyncio
    async def test_stop_ending_still_flushes_pending_calls(self, provider):
        """Clean stop ending → partial-flush behavior unchanged: the pending
        (incomplete) call still rides the response with recovered arguments."""
        chunks = [
            self._tool_chunk(
                [self._tool_call_delta(0, "call_1", "search", '{"query": "test"}')]
            ),
            # Unrepairably truncated → flushed as the call with empty args
            self._tool_chunk(
                [self._tool_call_delta(1, "call_2", "send_to_agent", '{"target_agent":"reviewer","content":"hel')]
            ),
            self._tool_chunk([], finish_reason="stop"),
        ]
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks(chunks)
        )

        result = await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="search")],
        )

        assert result.finish_reason == FinishReason.STOP.value
        assert [tc.tool_name for tc in result.tool_calls] == ["search", "send_to_agent"]
        assert isinstance(result.tool_calls[1].arguments, dict)


class TestOpenAIProviderCacheControl:
    """OpenAIProvider prompt_cache_key parameter tests.

    Verifies that ``prompt_cache_key`` passed via kwargs is injected into
    the API request params via ``inject_cache_control``, following the
    same pattern as ``reasoning_effort``.
    """

    @pytest.fixture
    def provider(self):
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(request_timeout_seconds=10, stream_idle_timeout_seconds=30),
            turn=TurnTimeoutPolicy(),
        )
        with patch("modex_agent.providers.openai_provider.AsyncOpenAI") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            p = OpenAIProvider(model="gpt-4o", api_key="sk-test", safety=safety)
            p._client = mock_client
            yield p

    def test_prompt_cache_key_injected_from_kwargs(self, provider):
        """_build_params pops prompt_cache_key from kwargs and injects it."""
        params = provider._build_params(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            prompt_cache_key="test-session-123",
        )
        assert params[PROMPT_CACHE_KEY_PARAM] == "test-session-123"

    def test_prompt_cache_key_omitted_when_not_in_kwargs(self, provider):
        """No prompt_cache_key in kwargs → param absent."""
        params = provider._build_params(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
        )
        assert PROMPT_CACHE_KEY_PARAM not in params

    def test_prompt_cache_key_omitted_when_empty(self, provider):
        """Empty session_id → param absent (inject_cache_control guard)."""
        params = provider._build_params(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            prompt_cache_key="",
        )
        assert PROMPT_CACHE_KEY_PARAM not in params

    def test_prompt_cache_key_not_duplicated_in_kwargs(self, provider):
        """prompt_cache_key is popped from kwargs, not merged twice by params.update."""
        params = provider._build_params(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            prompt_cache_key="session-abc",
        )
        assert list(params.keys()).count(PROMPT_CACHE_KEY_PARAM) == 1

    @pytest.mark.asyncio
    async def test_prompt_cache_key_flows_to_api_call(self, provider):
        """End-to-end: chat_stream passes prompt_cache_key to the SDK call."""
        chunks = [self._make_chunk(content="ok", finish_reason="stop")]
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks(chunks)
        )

        async def _on_content(d: str) -> None:
            pass

        async def _on_reasoning(d: str) -> None:
            pass

        await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            on_content_delta=_on_content,
            on_reasoning_delta=_on_reasoning,
            prompt_cache_key="e2e-session-id",
        )

        call_kwargs = provider._client.chat.completions.create.call_args.kwargs
        assert call_kwargs[PROMPT_CACHE_KEY_PARAM] == "e2e-session-id"

    def _make_chunk(self, content=None, finish_reason=None, reasoning=None, usage=None):
        """Build a mock ChatCompletionChunk."""
        delta = MagicMock()
        delta.content = content
        delta.tool_calls = None
        delta.model_extra = {"reasoning_content": reasoning} if reasoning else None

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = finish_reason

        chunk = MagicMock()
        chunk.choices = [choice]
        chunk.usage = usage
        return chunk

    async def _stream_chunks(self, chunks):
        for c in chunks:
            yield c


class TestTemperatureConfigChain:
    """Constructor temperature must reach the API when the call omits it.

    Regression: chat/chat_stream defaulted ``temperature=0.7``, so the
    provider-constructor value (e.g. model.yml ``temperature: 1.0``) never
    reached the API — every call silently sent 0.7. Defaults are now None:
    None falls back to the constructor value, an explicit per-call value wins.
    """

    @pytest.fixture
    def make_provider(self):
        def _make(temperature=None):
            safety = RuntimeSafetyPolicy(
                llm=LLMTimeoutPolicy(request_timeout_seconds=10, stream_idle_timeout_seconds=30),
                turn=TurnTimeoutPolicy(),
            )
            with patch("modex_agent.providers.openai_provider.AsyncOpenAI") as mock_client_cls:
                mock_client = MagicMock()
                mock_client_cls.return_value = mock_client
                kwargs = {"model": "gpt-4o", "api_key": "sk-test", "safety": safety}
                if temperature is not None:
                    kwargs["temperature"] = temperature
                p = OpenAIProvider(**kwargs)
                p._client = mock_client
            mock_client.chat.completions.create = AsyncMock(
                return_value=self._stream_chunks(
                    [self._make_chunk(content="ok", finish_reason="stop")]
                )
            )
            return p

        return _make

    def _make_chunk(self, content=None, finish_reason=None, reasoning=None, usage=None):
        """Build a mock ChatCompletionChunk."""
        delta = MagicMock()
        delta.content = content
        delta.tool_calls = None
        delta.model_extra = {"reasoning_content": reasoning} if reasoning else None

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = finish_reason

        chunk = MagicMock()
        chunk.choices = [choice]
        chunk.usage = usage
        return chunk

    async def _stream_chunks(self, chunks):
        for c in chunks:
            yield c

    @pytest.mark.asyncio
    async def test_ctor_temperature_reaches_api_when_call_omits_it(self, make_provider):
        provider = make_provider(temperature=1.0)
        await provider.chat_stream(messages=[ChatMessage(role=MessageRole.USER, content="hi")])
        call_kwargs = provider._client.chat.completions.create.call_args.kwargs
        assert call_kwargs["temperature"] == 1.0

    @pytest.mark.asyncio
    async def test_explicit_temperature_overrides_ctor(self, make_provider):
        provider = make_provider(temperature=1.0)
        await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            temperature=0.2,
        )
        call_kwargs = provider._client.chat.completions.create.call_args.kwargs
        assert call_kwargs["temperature"] == 0.2

    @pytest.mark.asyncio
    async def test_ctor_default_temperature_preserved(self, make_provider):
        provider = make_provider()  # ctor default 0.7
        await provider.chat_stream(messages=[ChatMessage(role=MessageRole.USER, content="hi")])
        call_kwargs = provider._client.chat.completions.create.call_args.kwargs
        assert call_kwargs["temperature"] == 0.7

    @pytest.mark.asyncio
    async def test_chat_path_ctor_temperature_reaches_api(self, make_provider):
        """chat() (ABC default None) must also fall back to the ctor value."""
        provider = make_provider(temperature=1.0)
        await provider.chat(messages=[ChatMessage(role=MessageRole.USER, content="hi")])
        call_kwargs = provider._client.chat.completions.create.call_args.kwargs
        assert call_kwargs["temperature"] == 1.0

    @pytest.mark.asyncio
    async def test_chat_stream_with_retry_ctor_temperature_reaches_api(self, make_provider):
        provider = make_provider(temperature=1.0)
        await provider.chat_stream_with_retry(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            max_retries=0,
        )
        call_kwargs = provider._client.chat.completions.create.call_args.kwargs
        assert call_kwargs["temperature"] == 1.0
