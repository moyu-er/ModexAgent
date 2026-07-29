"""Tests for ReActAgent unified streaming and non-streaming paths.

验证 ReActAgent 的统一执行循环：
- 流式与非流式共享同一主循环
- _request_llm 根据 emitter.wants_streaming() 选择正确路径
- 内容通过 get_content()、推理通过 get_reasoning() 被 _BufferingEmitter 收集
- 生命周期事件 (MODEL_OUTPUT, MODEL_REASONING) 仍然被分发
"""

from enum import Enum
from typing import Any, TypeVar
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from modex_agent.adapters.platform import StreamingMode
from modex_agent.agents.react import ReActAgent, ReActEvent
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult, ContentEmitter, EmitterConfig, StreamingAwareEmitter
from modex_agent.core.events import AgentEvent
from modex_agent.core.provider import StreamingLLMProvider
from modex_agent.core.tool_manager import ToolResult
from modex_agent.core.types import LLMResponse, ToolCall
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.core.session_id import SessionInfo
from modex_agent.agents.react.state import ReActTurnState


E = TypeVar('E', bound=AgentEvent)


class _BufferingEmitter(ContentEmitter[E]):
    """Minimal test emitter that captures output for assertions."""

    def __init__(self, config: EmitterConfig | None = None):
        super().__init__(config)
        self._buffer = ""
        self._reasoning_buffer = ""
        self._result: AgentResult | None = None
        self._events: list[tuple[E, Any]] = []

    async def emit(self, event: E, data: Any = None) -> None:
        event_name = event.value if isinstance(event, Enum) else str(event)
        if self.config.is_enabled(event_name):
            self._events.append((event, data))
        await super().emit(event, data)

    async def _on_event(self, event: E, data: Any = None) -> None:
        event_name = event.value if isinstance(event, Enum) else str(event)
        if event_name == "model_reasoning":
            if isinstance(data, str):
                self._reasoning_buffer += data

    async def emit_delta(self, delta: str) -> None:
        self._buffer += delta

    async def emit_content(self, full_content: str) -> None:
        self._buffer += full_content

    async def emit_complete(self, result: AgentResult) -> None:
        self._result = result

    async def emit_error(self, error: str) -> None:
        self._result = AgentResult(error=error, stop_reason=StopReason.ERROR)

    def get_content(self) -> str:
        return self._buffer

    def get_reasoning(self) -> str:
        return self._reasoning_buffer

    def get_events(self, event_type: E | None = None) -> list[tuple[E, Any]]:
        if event_type is not None:
            return [(e, d) for e, d in self._events if e == event_type]
        return list(self._events)

    def get_events_by_name(self, name: str) -> list[tuple[E, Any]]:
        result = []
        for e, d in self._events:
            if isinstance(e, Enum) and e.name == name or isinstance(e, str) and e == name:
                result.append((e, d))
        return result


def _make_runtime():
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="test", session=SessionInfo.from_str("s1"), turn_id="t1"),
        agent_kind=AgentKind.REACT, phase=TurnPhase.CREATED,
    )
    return AgentRuntime(services=AgentRuntimeServices(), state=state)


class MockNonStreamingProvider:
    """Mock LLMProvider for non-streaming tests (does not inherit from LLMProvider)."""

    async def chat(self, messages, **kwargs):
        return LLMResponse(content="Non-streaming response")

    def get_default_model(self):
        return "mock-model"


class MockStreamingProvider(StreamingLLMProvider):
    """Mock StreamingLLMProvider for testing."""

    def __init__(self):
        self._stream_content = None
        self._stream_reasoning = None
        self._stream_tool_calls = None

    async def chat(self, messages, **kwargs):
        return LLMResponse(content="Non-streaming response")

    async def chat_stream(self, messages, on_content_delta=None, on_reasoning_delta=None, **kwargs):
        content_parts = list(self._stream_content) if self._stream_content else []
        reasoning_parts = list(self._stream_reasoning) if self._stream_reasoning else []

        for chunk in content_parts:
            if on_content_delta:
                await on_content_delta(chunk)
        for chunk in reasoning_parts:
            if on_reasoning_delta:
                await on_reasoning_delta(chunk)

        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts) if reasoning_parts else None
        tool_calls = list(self._stream_tool_calls) if self._stream_tool_calls else []

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            reasoning_content=reasoning,
        )

    def get_default_model(self):
        return "mock-model"


class StreamingEmitter(_BufferingEmitter[ReActEvent]):
    def wants_streaming(self):
        return True


class TestReActAgentUnifiedLoop:
    """ReActAgent 统一循环测试（流式 + 非流式共享路径）。"""

    @pytest.fixture
    def streaming_provider(self):
        return MockStreamingProvider()

    @pytest.fixture
    def non_streaming_provider(self):
        return MockNonStreamingProvider()

    @pytest.fixture
    def context(self):
        runtime = _make_runtime()
        return AgentContext(
            system_prompt="You are a helpful assistant.",
            history=ListMessageHistory([{"role": "user", "content": "Hello"}]),
            tool_manager=MagicMock(),
            max_iterations=3,
            identity=runtime.state.identity, runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )

    @pytest.fixture
    def emitter(self):
        return _BufferingEmitter[ReActEvent]()

    @pytest.fixture
    def streaming_emitter(self):
        return StreamingEmitter()

    # ========================================================================
    # Streaming mode (emitter wants streaming)
    # ========================================================================

    @pytest.mark.asyncio
    async def test_streaming_basic_response(self, streaming_provider, context, streaming_emitter):
        streaming_provider._stream_content = ["Hello ", "World"]
        agent = ReActAgent(provider=streaming_provider)

        result = await agent.run(context, streaming_emitter)

        assert result.content == "Hello World"
        assert result.stop_reason == "completed"
        assert streaming_emitter.get_content() == "Hello World"

    @pytest.mark.asyncio
    async def test_streaming_with_reasoning(self, streaming_provider, context, streaming_emitter):
        streaming_provider._stream_reasoning = ["Let me think... ", "I got it!"]
        streaming_provider._stream_content = ["The answer is 42"]
        agent = ReActAgent(provider=streaming_provider)

        result = await agent.run(context, streaming_emitter)

        assert result.content == "The answer is 42"
        assert result.reasoning == "Let me think... I got it!"
        assert streaming_emitter.get_reasoning() == "Let me think... I got it!"

    @pytest.mark.asyncio
    async def test_streaming_with_tool_call(self, streaming_provider, context, streaming_emitter):
        tool_call = ToolCall(tool_name="weather", arguments={"city": "Beijing"}, call_id="call_1")
        iteration = 0

        async def mock_chat_stream(*args, **kwargs):
            nonlocal iteration
            iteration += 1
            on_content_delta = kwargs.get("on_content_delta")
            if iteration == 1:
                if on_content_delta:
                    await on_content_delta("")
                return LLMResponse(content="", tool_calls=[tool_call])
            else:
                if on_content_delta:
                    await on_content_delta("Sunny in Beijing")
                return LLMResponse(content="Sunny in Beijing")

        streaming_provider.chat_stream = mock_chat_stream
        context.tool_manager.execute = AsyncMock(return_value=ToolResult.from_text("weather", "Sunny, 25C"))
        agent = ReActAgent(provider=streaming_provider)

        result = await agent.run(context, streaming_emitter)

        context.tool_manager.execute.assert_called_once_with("weather", {"city": "Beijing"}, ctx=ANY)
        assert "Sunny in Beijing" in result.content
        assert len(result.messages) == 3

    @pytest.mark.asyncio
    async def test_streaming_event_sequence(self, streaming_provider, context, streaming_emitter):
        streaming_provider._stream_content = ["Thinking"]
        agent = ReActAgent(provider=streaming_provider)

        await agent.run(context, streaming_emitter)
        assert len(streaming_emitter.get_events()) > 0

    @pytest.mark.asyncio
    async def test_streaming_calls_after_llm_response_hook(self, streaming_provider, context, streaming_emitter):
        responses: list[str | None] = []

        from modex_agent.hook.abc import AfterLLMResponseHook

        class TrackingHook(AfterLLMResponseHook):
            @property
            def name(self) -> str:
                return "tracking"

            async def after_llm_response(self, ctx, response):
                responses.append(response.content)

        streaming_provider._stream_content = ["Hello ", "World"]
        from modex_agent.hook import HookRunner, HookSpec, HookErrorPolicy
        context.runtime.services.hooks = HookRunner([
            HookSpec(hook=TrackingHook(), on_error=HookErrorPolicy.LOG)
        ])
        agent = ReActAgent(provider=streaming_provider)

        await agent.run(context, streaming_emitter)

        assert responses == ["Hello World"]

    @pytest.mark.asyncio
    async def test_streaming_delta_emitted_as_independent_chunks(self, streaming_provider, context, streaming_emitter):
        streaming_provider._stream_content = ["Hello ", "World"]
        agent = ReActAgent(provider=streaming_provider)

        await agent.run(context, streaming_emitter)

        output_events = streaming_emitter.get_events_by_name("MODEL_OUTPUT")
        assert len(output_events) == 2
        assert output_events[0][1] == "Hello "
        assert output_events[1][1] == "World"

    @pytest.mark.asyncio
    async def test_streaming_reasoning_emitted_as_independent_chunks(self, streaming_provider, context, streaming_emitter):
        streaming_provider._stream_reasoning = ["Think ", "hard"]
        streaming_provider._stream_content = ["42"]
        agent = ReActAgent(provider=streaming_provider)

        await agent.run(context, streaming_emitter)

        reasoning_events = streaming_emitter.get_events_by_name("MODEL_REASONING")
        assert len(reasoning_events) == 2
        assert reasoning_events[0][1] == "Think "
        assert reasoning_events[1][1] == "hard"

    @pytest.mark.asyncio
    async def test_streaming_max_iterations(self, streaming_provider, context, streaming_emitter):
        context.max_iterations = 1
        tool_call = ToolCall(tool_name="dummy", arguments={}, call_id="call_1")

        async def mock_chat_stream(*args, **kwargs):
            return LLMResponse(content="", tool_calls=[tool_call])

        streaming_provider.chat_stream = mock_chat_stream
        context.tool_manager.execute = AsyncMock(return_value=ToolResult.from_text("dummy", "done"))
        agent = ReActAgent(provider=streaming_provider)

        result = await agent.run(context, streaming_emitter)
        assert result.stop_reason == "max_iterations"

    @pytest.mark.asyncio
    async def test_streaming_error_handling(self, streaming_provider, context, streaming_emitter):
        async def mock_chat_stream(*args, **kwargs):
            raise ValueError("Stream error")

        streaming_provider.chat_stream = mock_chat_stream
        agent = ReActAgent(provider=streaming_provider)

        result = await agent.run(context, streaming_emitter)
        assert result.stop_reason == "error"
        assert "Stream error" in result.error

    # ========================================================================
    # Non-streaming mode (emitter does not want streaming)
    # ========================================================================

    @pytest.mark.asyncio
    async def test_non_streaming_basic_response(self, non_streaming_provider, context, emitter):
        async def mock_chat(*args, **kwargs):
            return LLMResponse(content="Hello from non-streaming")

        non_streaming_provider.chat = mock_chat
        agent = ReActAgent(provider=non_streaming_provider)

        result = await agent.run(context, emitter)

        assert result.content == "Hello from non-streaming"
        assert result.stop_reason == "completed"
        assert emitter.get_content() == "Hello from non-streaming"

    @pytest.mark.asyncio
    async def test_non_streaming_with_reasoning(self, non_streaming_provider, context, emitter):
        async def mock_chat(*args, **kwargs):
            return LLMResponse(
                content="The answer is 42",
                reasoning_content="Let me calculate... 20 + 22 = 42",
            )

        non_streaming_provider.chat = mock_chat
        agent = ReActAgent(provider=non_streaming_provider)

        result = await agent.run(context, emitter)

        assert result.content == "The answer is 42"
        assert result.reasoning == "Let me calculate... 20 + 22 = 42"
        assert emitter.get_reasoning() == "Let me calculate... 20 + 22 = 42"

    @pytest.mark.asyncio
    async def test_non_streaming_event_emission(self, non_streaming_provider, context, emitter):
        async def mock_chat(*args, **kwargs):
            return LLMResponse(content="Complete response")

        non_streaming_provider.chat = mock_chat
        agent = ReActAgent(provider=non_streaming_provider)

        await agent.run(context, emitter)

        events = emitter.get_events_by_name("MODEL_OUTPUT")
        assert len(events) == 1
        assert events[0][1] == "Complete response"

    @pytest.mark.asyncio
    async def test_non_streaming_calls_after_llm_response_hook(self, non_streaming_provider, context, emitter):
        responses: list[str | None] = []

        from modex_agent.hook.abc import AfterLLMResponseHook

        class TrackingHook(AfterLLMResponseHook):
            @property
            def name(self) -> str:
                return "tracking"

            async def after_llm_response(self, ctx, response):
                responses.append(response.content)

        async def mock_chat(*args, **kwargs):
            return LLMResponse(content="Complete response")

        non_streaming_provider.chat = mock_chat
        from modex_agent.hook import HookRunner, HookSpec, HookErrorPolicy
        context.runtime.services.hooks = HookRunner([
            HookSpec(hook=TrackingHook(), on_error=HookErrorPolicy.LOG)
        ])
        agent = ReActAgent(provider=non_streaming_provider)

        await agent.run(context, emitter)

        assert responses == ["Complete response"]

    @pytest.mark.asyncio
    async def test_non_streaming_with_tool_call(self, non_streaming_provider, context, emitter):
        iteration = 0

        async def mock_chat(*args, **kwargs):
            nonlocal iteration
            iteration += 1
            if iteration == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCall(
                        tool_name="weather",
                        arguments={"city": "Beijing"},
                        call_id="call_1",
                    )],
                )
            else:
                return LLMResponse(content="It's sunny in Beijing")

        non_streaming_provider.chat = mock_chat
        context.tool_manager.execute = AsyncMock(return_value=ToolResult.from_text("weather", "Sunny, 25C"))
        agent = ReActAgent(provider=non_streaming_provider)

        result = await agent.run(context, emitter)

        context.tool_manager.execute.assert_called_once_with("weather", {"city": "Beijing"}, ctx=ANY)
        assert "sunny in Beijing" in result.content
        assert len(result.messages) == 3

    @pytest.mark.asyncio
    async def test_non_streaming_not_using_chat_stream(self, non_streaming_provider, context, emitter):
        chat_called = False
        chat_stream_called = False

        async def mock_chat(*args, **kwargs):
            nonlocal chat_called
            chat_called = True
            return LLMResponse(content="Response")

        async def mock_chat_stream(*args, **kwargs):
            nonlocal chat_stream_called
            chat_stream_called = True
            return LLMResponse(content="Response")

        non_streaming_provider.chat = mock_chat
        # _BufferingEmitter wants_streaming returns False by default
        assert emitter.wants_streaming() is False
        agent = ReActAgent(provider=non_streaming_provider)

        await agent.run(context, emitter)

        assert chat_called is True
        assert chat_stream_called is False

    @pytest.mark.asyncio
    async def test_non_streaming_max_iterations(self, non_streaming_provider, context, emitter):
        context.max_iterations = 1

        async def mock_chat(*args, **kwargs):
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(tool_name="dummy", arguments={}, call_id="call_1")],
            )

        non_streaming_provider.chat = mock_chat
        context.tool_manager.execute = AsyncMock(return_value=ToolResult.from_text("dummy", "done"))
        agent = ReActAgent(provider=non_streaming_provider)

        result = await agent.run(context, emitter)
        assert result.stop_reason == "max_iterations"

    @pytest.mark.asyncio
    async def test_non_streaming_error_handling(self, non_streaming_provider, context, emitter):
        async def mock_chat(*args, **kwargs):
            raise ValueError("API error")

        non_streaming_provider.chat = mock_chat
        agent = ReActAgent(provider=non_streaming_provider)

        result = await agent.run(context, emitter)
        assert result.stop_reason == "error"
        assert "API error" in result.error

    @pytest.mark.asyncio
    async def test_non_streaming_graceful_without_reasoning(self, non_streaming_provider, context, emitter):
        async def mock_chat(*args, **kwargs):
            return LLMResponse(content="Simple response")

        non_streaming_provider.chat = mock_chat
        agent = ReActAgent(provider=non_streaming_provider)

        result = await agent.run(context, emitter)
        assert result.content == "Simple response"
        assert result.reasoning is None


class TestReActAgentRegression:
    """回归测试：验证重构后的关键行为。"""

    @pytest.fixture
    def streaming_provider(self):
        return MockStreamingProvider()

    @pytest.fixture
    def non_streaming_provider(self):
        return MockNonStreamingProvider()

    @pytest.fixture
    def context(self):
        runtime = _make_runtime()
        return AgentContext(
            system_prompt="You are a helpful assistant.",
            history=ListMessageHistory([{"role": "user", "content": "Hello"}]),
            tool_manager=MagicMock(),
            max_iterations=3,
            identity=runtime.state.identity, runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )

    @pytest.mark.asyncio
    async def test_pseudo_streaming_flushes_on_emit_stream_end_resuming(self, streaming_provider, context):
        """Regression: pseudo-streaming 模式下，emit_stream_end(resuming=True) 会刷新缓冲区。"""
        tool_call = ToolCall(tool_name="weather", arguments={"city": "Beijing"}, call_id="call_1")

        async def mock_chat_stream(*args, **kwargs):
            on_content_delta = kwargs.get("on_content_delta")
            if on_content_delta:
                await on_content_delta("Let me check...")
            return LLMResponse(content="Let me check...", tool_calls=[tool_call])

        streaming_provider.chat_stream = mock_chat_stream
        context.tool_manager.execute = AsyncMock(return_value=ToolResult.from_text("weather", "Sunny"))

        class MockAdapter:
            streaming_mode = StreamingMode.PSEUDO
            def __init__(self):
                self.send_calls = []
            async def send(self, message, session_id):
                self.send_calls.append((message.content, session_id))
            async def send_delta(self, delta, session_id):
                pass
            async def flush_deltas(self, session_id):
                pass

        class PseudoStreamingEmitter(StreamingAwareEmitter[ReActEvent]):
            def wants_streaming(self):
                return True

        adapter = MockAdapter()
        emitter = PseudoStreamingEmitter(
            output_adapter=adapter,
            session_id="test_session",
        )
        agent = ReActAgent(provider=streaming_provider)

        await agent.run(context, emitter)

        assert any("Let me check..." in call[0] for call in adapter.send_calls)

    @pytest.mark.asyncio
    async def test_default_emitter_does_not_leak_reasoning_to_content(self, streaming_provider, context):
        """Regression: 默认 emitter 不会将 reasoning 混入 content buffer。"""
        streaming_provider._stream_reasoning = ["Thinking..."]
        streaming_provider._stream_content = ["Answer"]
        emitter = StreamingEmitter()
        agent = ReActAgent(provider=streaming_provider)

        await agent.run(context, emitter)

        assert emitter.get_content() == "Answer"
        assert emitter.get_reasoning() == "Thinking..."

    @pytest.mark.asyncio
    async def test_non_streaming_path_calls_emit_content_not_emit_delta(self, non_streaming_provider, context):
        """Regression: 非流式路径调用 emit_content() 而不是 emit_delta()。"""
        async def mock_chat(*args, **kwargs):
            return LLMResponse(content="Full response")

        non_streaming_provider.chat = mock_chat

        class TrackingEmitter(_BufferingEmitter[ReActEvent]):
            def __init__(self):
                super().__init__()
                self.content_calls = []
                self.delta_calls = []

            async def emit_content(self, full_content: str) -> None:
                self.content_calls.append(full_content)
                await super().emit_content(full_content)

            async def emit_delta(self, delta: str) -> None:
                self.delta_calls.append(delta)
                await super().emit_delta(delta)

        emitter = TrackingEmitter()
        agent = ReActAgent(provider=non_streaming_provider)

        await agent.run(context, emitter)

        assert emitter.content_calls == ["Full response"]
        assert emitter.delta_calls == []

    @pytest.mark.asyncio
    async def test_history_persists_per_iteration(self, streaming_provider, context):
        """Regression: ReActAgent 应在每次迭代时将消息追加到 context.history。"""
        tool_call = ToolCall(tool_name="weather", arguments={"city": "Beijing"}, call_id="call_1")
        iteration = 0

        async def mock_chat_stream(*args, **kwargs):
            nonlocal iteration
            iteration += 1
            on_content_delta = kwargs.get("on_content_delta")
            if iteration == 1:
                if on_content_delta:
                    await on_content_delta("")
                return LLMResponse(content="", tool_calls=[tool_call])
            else:
                if on_content_delta:
                    await on_content_delta("Sunny in Beijing")
                return LLMResponse(content="Sunny in Beijing")

        streaming_provider.chat_stream = mock_chat_stream
        context.tool_manager.execute = AsyncMock(return_value=ToolResult.from_text("weather", "Sunny, 25C"))

        agent = ReActAgent(provider=streaming_provider)
        emitter = StreamingEmitter()

        result = await agent.run(context, emitter)

        history = await context.history.to_list()
        assert len(history) == 4  # user + assistant(tool) + tool + assistant(final)
        assert history[1]["role"] == "assistant"
        assert history[1].get("tool_calls")
        assert history[2]["role"] == "tool"
        assert history[3]["role"] == "assistant"
        assert "Sunny in Beijing" in history[3]["content"]
        assert len(result.messages) == 3


class TestReActAgentCheckpoint:
    """Crash recovery checkpoint tests — now using TurnSnapshot.message_delta."""

    @pytest.fixture
    def streaming_provider(self):
        return MockStreamingProvider()

    @pytest.fixture
    def non_streaming_provider(self):
        return MockNonStreamingProvider()

    @pytest.fixture
    def context(self):
        runtime = _make_runtime()
        return AgentContext(
            system_prompt="You are a helpful assistant.",
            history=ListMessageHistory([{"role": "user", "content": "Hello"}]),
            tool_manager=MagicMock(),
            max_iterations=3,
            identity=runtime.state.identity, runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )

    @pytest.fixture
    def emitter(self):
        return _BufferingEmitter[ReActEvent]()

    @pytest.mark.asyncio
    async def test_checkpoint_saved_after_assistant_and_tool_messages(self, streaming_provider, context, emitter):
        tool_call = ToolCall(tool_name="weather", arguments={"city": "Beijing"}, call_id="call_1")
        iteration = 0

        async def mock_chat_stream(*args, **kwargs):
            nonlocal iteration
            iteration += 1
            on_content_delta = kwargs.get("on_content_delta")
            if iteration == 1:
                if on_content_delta:
                    await on_content_delta("")
                return LLMResponse(content="", tool_calls=[tool_call])
            else:
                if on_content_delta:
                    await on_content_delta("Sunny in Beijing")
                return LLMResponse(content="Sunny in Beijing")

        streaming_provider.chat_stream = mock_chat_stream
        context.tool_manager.execute = AsyncMock(return_value=ToolResult.from_text("weather", "Sunny, 25C"))

        agent = ReActAgent(provider=streaming_provider)
        streaming_emitter = StreamingEmitter()

        result = await agent.run(context, streaming_emitter)

        # message_delta tracks both assistant and tool messages
        from modex_agent.runtime.services import require_runtime_state
        from modex_agent.agents.react.state import ReActTurnState
        state = require_runtime_state(context.runtime, ReActTurnState)
        assert len(state.message_delta) >= 2, f"expected >= 2 message_delta entries, got {len(state.message_delta)}"

        # Final content is correct
        assert "Sunny in Beijing" in result.content

    @pytest.mark.asyncio
    async def test_checkpoint_cleared_on_final_output(self, non_streaming_provider, context, emitter):
        async def mock_chat(*args, **kwargs):
            return LLMResponse(content="Final answer")

        non_streaming_provider.chat = mock_chat

        agent = ReActAgent(provider=non_streaming_provider)
        await agent.run(context, emitter)

        # Turn completed successfully — phase is COMPLETED
        from modex_agent.runtime.enums import TurnPhase
        assert context.runtime.state.phase == TurnPhase.COMPLETED

        # message_delta records the assistant message
        assert len(context.runtime.state.message_delta) >= 1

    @pytest.mark.asyncio
    async def test_checkpoint_saved_on_error(self, non_streaming_provider, context, emitter):
        async def mock_chat(*args, **kwargs):
            raise ValueError("LLM failure")

        non_streaming_provider.chat = mock_chat

        agent = ReActAgent(provider=non_streaming_provider)
        result = await agent.run(context, emitter)

        # Error result preserves messages for crash recovery
        assert result.stop_reason == "error"
        assert result.error is not None
        assert "LLM failure" in str(result.error)

    @pytest.mark.asyncio
    async def test_multiturn_tool_calls_synced_to_context_history(self, streaming_provider, context):
        """Regression: 多轮 ReAct 中 assistant_message 和 tool_message 必须同步回 context.history。"""
        tool_call = ToolCall(tool_name="weather", arguments={"city": "Beijing"}, call_id="call_1")
        iteration = 0

        async def mock_chat_stream(*args, **kwargs):
            nonlocal iteration
            iteration += 1
            on_content_delta = kwargs.get("on_content_delta")
            if iteration == 1:
                if on_content_delta:
                    await on_content_delta("")
                return LLMResponse(content="", tool_calls=[tool_call])
            else:
                if on_content_delta:
                    await on_content_delta("Sunny in Beijing")
                return LLMResponse(content="Sunny in Beijing")

        streaming_provider.chat_stream = mock_chat_stream
        context.tool_manager.execute = AsyncMock(return_value=ToolResult.from_text("weather", "Sunny, 25C"))

        emitter = StreamingEmitter()
        agent = ReActAgent(provider=streaming_provider)

        await agent.run(context, emitter)

        history = await context.history.to_list()
        # history 应包含用户原始消息 + assistant(tool_call) + tool(result) + assistant(final)
        assert len(history) == 4
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"
        assert history[1].get("tool_calls")
        assert history[2]["role"] == "tool"
        assert history[3]["role"] == "assistant"
        assert "Sunny in Beijing" in (history[3]["content"] or "")
