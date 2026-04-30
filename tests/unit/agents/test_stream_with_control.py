"""Tests for ReActAgent._stream_with_control — LLM_STREAM interceptor path."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.agents.react.agent import ReActEvent, ReActAgent
from framework.core.provider import StreamingLLMProvider
from framework.core.types import LLMResponse, ToolCall
from framework.interceptor.abc import InterceptorScope


class _StreamingEmitter:
    """Emitter that wants streaming and captures events."""

    def __init__(self):
        self.events: list = []
        self.deltas: list[str] = []
        self._stream_end_resuming: bool | None = None

    def wants_streaming(self) -> bool:
        return True

    async def emit(self, event, data=None):
        self.events.append((event, data))

    async def emit_delta(self, delta: str):
        if delta:
            self.deltas.append(delta)

    async def emit_content(self, full: str):
        if full:
            self.deltas.append(full)

    async def emit_stream_end(self, resuming: bool = False):
        self._stream_end_resuming = resuming

    async def emit_complete(self, result):
        pass

    async def emit_error(self, error: str):
        pass


class _FakeHistory:
    def __init__(self):
        self.messages: list = []

    async def append(self, message):
        self.messages.append(message)

    async def replace_all(self, messages):
        self.messages = list(messages)

    def __iter__(self):
        return iter(self.messages)


class _FakeContext:
    def __init__(self, *, interceptor_chain=None):
        self.messages = [{"role": "user", "content": "run a tool"}]
        self.history = _FakeHistory()
        self.hooks: list = []
        self.max_iterations = 3
        self.max_tools_per_turn = None
        self.attachments: list = []
        self.tool_manager = None
        self.temperature = 0.7
        self.max_tokens = None
        self.governance = None
        self.on_checkpoint = None
        self.metadata: dict = {}
        self.session_id = "test-session-001"
        self.interceptor_chain = interceptor_chain
        self.checkpoint_store = None

    async def to_messages(self):
        return list(self.messages)

    def get_tool_descriptions(self):
        return None


class TestStreamWithControlPreservesToolCalls:
    """P0-1r2: _stream_with_control MUST preserve tool_calls from LLM response."""

    async def test_preserves_tool_calls_when_llm_returns_them(self):
        """When LLM returns tool_calls via chat_stream, _stream_with_control
        must include them in the returned LLMResponse."""
        # Arrange: mock interceptor_chain with has_scope(LLM_STREAM)=True
        from framework.interceptor.abc import LLMStreamChunk, LLMStreamContext

        async def _fake_llm_stream(ctx, call, actual_stream):
            """Mirror the actual stream, passing chunks through."""
            async for chunk in actual_stream():
                yield chunk

        fake_chain = MagicMock()
        fake_chain.has_scope = MagicMock(return_value=True)
        fake_chain.around_llm_stream = _fake_llm_stream

        # Arrange: mock streaming provider that returns tool_calls
        tool_call = ToolCall(tool_name="shell", arguments={"cmd": "ls"}, call_id="call_1")

        class StreamingProvider(StreamingLLMProvider):
            async def chat_stream(self, messages, tools=None, temperature=0.7,
                                  max_tokens=None, on_content_delta=None,
                                  on_reasoning_delta=None, **kwargs):
                if on_content_delta:
                    await on_content_delta("Let me run a command.")
                return LLMResponse(
                    content="Let me run a command.",
                    finish_reason="tool_calls",
                    tool_calls=[tool_call],
                )

            async def chat(self, *args, **kwargs) -> LLMResponse:
                return LLMResponse(content="", finish_reason="stop")

            def get_default_model(self) -> str:
                return "test-model"

        provider = StreamingProvider()
        agent = ReActAgent(provider=provider)
        emitter = _StreamingEmitter()
        ctx = _FakeContext(interceptor_chain=fake_chain)

        # Act
        result = await agent.run(ctx, emitter)

        # Assert: tool_calls MUST be in the result's messages
        # The assistant message in all_new_messages should contain tool_calls
        assert result.messages is not None, "Result should contain messages"
        assistant_msgs = [m for m in result.messages
                          if isinstance(m, dict) and m.get("role") == "assistant"
                          and m.get("tool_calls")]
        assert len(assistant_msgs) > 0, (
            f"Expected assistant message with tool_calls, got: {result.messages}"
        )

        # Assert: emitter should know this is a tool-call turn (not final)
        assert emitter._stream_end_resuming is True, (
            f"emit_stream_end should be called with resuming=True when tool_calls exist, "
            f"got resuming={emitter._stream_end_resuming}"
        )

    async def test_no_tool_calls_when_llm_returns_none(self):
        """When LLM returns NO tool_calls, _stream_with_control must work correctly."""
        from framework.interceptor.abc import LLMStreamChunk, LLMStreamContext

        async def _fake_llm_stream(ctx, call, actual_stream):
            async for chunk in actual_stream():
                yield chunk

        fake_chain = MagicMock()
        fake_chain.has_scope = MagicMock(return_value=True)
        fake_chain.around_llm_stream = _fake_llm_stream

        class StreamingProviderNoTools(StreamingLLMProvider):
            async def chat_stream(self, messages, tools=None, temperature=0.7,
                                  max_tokens=None, on_content_delta=None,
                                  on_reasoning_delta=None, **kwargs):
                if on_content_delta:
                    await on_content_delta("Hello!")
                return LLMResponse(
                    content="Hello!",
                    finish_reason="stop",
                    tool_calls=[],
                )

            async def chat(self, *args, **kwargs) -> LLMResponse:
                return LLMResponse(content="", finish_reason="stop")

            def get_default_model(self) -> str:
                return "test-model"

        provider = StreamingProviderNoTools()
        agent = ReActAgent(provider=provider)
        emitter = _StreamingEmitter()
        ctx = _FakeContext(interceptor_chain=fake_chain)

        result = await agent.run(ctx, emitter)

        assert result.content == "Hello!"
        assert emitter._stream_end_resuming is False
