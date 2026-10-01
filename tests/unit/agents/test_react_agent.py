"""Tests for ReActAgent unified streaming and non-streaming paths.

验证 ReActAgent 的统一执行循环：
- 流式与非流式共享同一主循环
- 路径选择依据 sink.wants_streaming()
- 内容通过 get_content()、推理通过 get_reasoning() 被 _BufferingSink 收集
- 生命周期事件 (text, reasoning, turn_finished …) 以核心 TurnEvent 分发
"""

from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from modex_agent.adapters.emitter import BufferingSink
from modex_agent.adapters.platform import StreamingMode
from modex_agent.agents.react import ReActAgent
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult, TurnEvent, TurnEventSink
from modex_agent.core.llm_struct import LLMResponse
from modex_agent.core.message import ToolCall
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import ToolResult
from modex_agent.core.turn.enums import AgentKind, TurnPhase
from modex_agent.core.turn.models import TurnIdentity
from modex_agent.core.turn_events import (
    StopReason,
    TurnFinishedEvent,
    TurnReasoningEvent,
    TurnTextEvent,
)
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices


class _BufferingSink(TurnEventSink):
    """Minimal test sink that captures output for assertions."""

    def __init__(self, streaming: bool = False):
        super().__init__()
        self._streaming = streaming
        self._buffer = ""
        self._reasoning_buffer = ""
        self._result: AgentResult | None = None
        self._events: list[TurnEvent] = []

    def wants_streaming(self) -> bool:
        return self._streaming

    async def _dispatch(self, event: TurnEvent) -> None:
        self._events.append(event)
        match event:
            case TurnTextEvent(text=text):
                self._buffer += text
            case TurnReasoningEvent(text=text):
                self._reasoning_buffer += text
            case TurnFinishedEvent(stop_reason=stop_reason, error=error):
                self._result = AgentResult(
                    error=error,
                    stop_reason=stop_reason,
                )

    def get_content(self) -> str:
        return self._buffer

    def get_reasoning(self) -> str:
        return self._reasoning_buffer

    def get_result(self) -> AgentResult | None:
        return self._result

    def get_events(self, kind: str | None = None) -> list[TurnEvent]:
        if kind is not None:
            return [e for e in self._events if e.kind == kind]
        return list(self._events)


def _make_runtime():
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="test", session=SessionInfo.from_str("s1"), turn_id="t1"),
        agent_kind=AgentKind.REACT, phase=TurnPhase.CREATED,
    )
    return AgentRuntime(services=AgentRuntimeServices(), state=state)


def _make_tool_manager():
    manager = MagicMock()
    manager.get_tool.return_value = None
    return manager


class MockNonStreamingProvider(CallbackStreamProvider):
    """Callback-style mock: only chat_stream is overridden (bridge path)."""

    def get_default_model(self):
        return "mock-model"

    async def chat_stream(self, messages, on_content_delta=None, on_reasoning_delta=None, **kwargs):
        return LLMResponse(content="Non-streaming response")


class MockStreamingProvider(CallbackStreamProvider):
    """Mock CallbackStreamProvider for testing."""

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


class StreamingEmitter(_BufferingSink):
    def __init__(self):
        super().__init__(streaming=True)


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
            tool_manager=_make_tool_manager(),
            max_iterations=3,
            identity=runtime.state.identity, runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )

    @pytest.fixture
    def emitter(self):
        return _BufferingSink()

    @pytest.fixture
    def streaming_emitter(self):
        return StreamingEmitter()

    # ========================================================================
    # Streaming mode (sink wants streaming)
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
        from modex_agent.hook import HookErrorPolicy, HookRunner, HookSpec
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

        output_events = streaming_emitter.get_events("text")
        assert [e.text for e in output_events] == ["Hello ", "World"]

    @pytest.mark.asyncio
    async def test_streaming_reasoning_emitted_as_independent_chunks(self, streaming_provider, context, streaming_emitter):
        streaming_provider._stream_reasoning = ["Think ", "hard"]
        streaming_provider._stream_content = ["42"]
        agent = ReActAgent(provider=streaming_provider)

        await agent.run(context, streaming_emitter)

        reasoning_events = streaming_emitter.get_events("reasoning")
        assert [e.text for e in reasoning_events] == ["Think ", "hard"]

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

        non_streaming_provider.chat_stream = mock_chat
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

        non_streaming_provider.chat_stream = mock_chat
        agent = ReActAgent(provider=non_streaming_provider)

        result = await agent.run(context, emitter)

        assert result.content == "The answer is 42"
        assert result.reasoning == "Let me calculate... 20 + 22 = 42"
        assert emitter.get_reasoning() == "Let me calculate... 20 + 22 = 42"

    @pytest.mark.asyncio
    async def test_non_streaming_event_emission(self, non_streaming_provider, context, emitter):
        async def mock_chat(*args, **kwargs):
            return LLMResponse(content="Complete response")

        non_streaming_provider.chat_stream = mock_chat
        agent = ReActAgent(provider=non_streaming_provider)

        await agent.run(context, emitter)

        events = emitter.get_events("text")
        assert [e.text for e in events] == ["Complete response"]

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

        non_streaming_provider.chat_stream = mock_chat
        from modex_agent.hook import HookErrorPolicy, HookRunner, HookSpec
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

        non_streaming_provider.chat_stream = mock_chat
        context.tool_manager.execute = AsyncMock(return_value=ToolResult.from_text("weather", "Sunny, 25C"))
        agent = ReActAgent(provider=non_streaming_provider)

        result = await agent.run(context, emitter)

        context.tool_manager.execute.assert_called_once_with("weather", {"city": "Beijing"}, ctx=ANY)
        assert "sunny in Beijing" in result.content
        assert len(result.messages) == 3

    @pytest.mark.asyncio
    async def test_non_streaming_not_using_chat(self, non_streaming_provider, context, emitter):
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
        non_streaming_provider.chat_stream = mock_chat_stream
        # _BufferingEmitter wants_streaming returns False by default — the
        # single event loop still reaches the provider via the bridge's
        # chat_stream, never via chat().
        assert emitter.wants_streaming() is False
        agent = ReActAgent(provider=non_streaming_provider)

        await agent.run(context, emitter)

        assert chat_called is False
        assert chat_stream_called is True

    @pytest.mark.asyncio
    async def test_non_streaming_max_iterations(self, non_streaming_provider, context, emitter):
        context.max_iterations = 1

        async def mock_chat(*args, **kwargs):
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(tool_name="dummy", arguments={}, call_id="call_1")],
            )

        non_streaming_provider.chat_stream = mock_chat
        context.tool_manager.execute = AsyncMock(return_value=ToolResult.from_text("dummy", "done"))
        agent = ReActAgent(provider=non_streaming_provider)

        result = await agent.run(context, emitter)
        assert result.stop_reason == "max_iterations"

    @pytest.mark.asyncio
    async def test_non_streaming_error_handling(self, non_streaming_provider, context, emitter):
        async def mock_chat(*args, **kwargs):
            raise ValueError("API error")

        non_streaming_provider.chat_stream = mock_chat
        agent = ReActAgent(provider=non_streaming_provider)

        result = await agent.run(context, emitter)
        assert result.stop_reason == "error"
        assert "API error" in result.error

    @pytest.mark.asyncio
    async def test_non_streaming_graceful_without_reasoning(self, non_streaming_provider, context, emitter):
        async def mock_chat(*args, **kwargs):
            return LLMResponse(content="Simple response")

        non_streaming_provider.chat_stream = mock_chat
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
            tool_manager=_make_tool_manager(),
            max_iterations=3,
            identity=runtime.state.identity, runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )

    @pytest.mark.asyncio
    async def test_pseudo_streaming_flushes_per_segment(self, streaming_provider, context):
        """Regression: SEGMENT 策略在 iteration_finished 边界刷新缓冲区。"""
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

        adapter = MockAdapter()
        emitter = BufferingSink(
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
    async def test_non_streaming_path_emits_one_folded_text_event(self, non_streaming_provider, context):
        """Regression: 非流式路径将折叠响应作为单条完整 text 事件一次性发出。"""
        async def mock_chat(*args, **kwargs):
            return LLMResponse(content="Full response")

        non_streaming_provider.chat_stream = mock_chat

        emitter = _BufferingSink()
        agent = ReActAgent(provider=non_streaming_provider)

        await agent.run(context, emitter)

        assert [e.text for e in emitter.get_events("text")] == ["Full response"]

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
            tool_manager=_make_tool_manager(),
            max_iterations=3,
            identity=runtime.state.identity, runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )

    @pytest.fixture
    def emitter(self):
        return _BufferingSink()

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
        from modex_agent.agents.react.state import ReActTurnState
        from modex_agent.runtime.services import require_runtime_state
        state = require_runtime_state(context.runtime, ReActTurnState)
        assert len(state.message_delta) >= 2, f"expected >= 2 message_delta entries, got {len(state.message_delta)}"

        # Final content is correct
        assert "Sunny in Beijing" in result.content

    @pytest.mark.asyncio
    async def test_checkpoint_cleared_on_final_output(self, non_streaming_provider, context, emitter):
        async def mock_chat(*args, **kwargs):
            return LLMResponse(content="Final answer")

        non_streaming_provider.chat_stream = mock_chat

        agent = ReActAgent(provider=non_streaming_provider)
        await agent.run(context, emitter)

        # Turn completed successfully — phase is COMPLETED
        from modex_agent.core.turn.enums import TurnPhase
        assert context.runtime.state.phase == TurnPhase.COMPLETED

        # message_delta records the assistant message
        assert len(context.runtime.state.message_delta) >= 1

    @pytest.mark.asyncio
    async def test_checkpoint_saved_on_error(self, non_streaming_provider, context, emitter):
        async def mock_chat(*args, **kwargs):
            raise ValueError("LLM failure")

        non_streaming_provider.chat_stream = mock_chat

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


class TestScriptedTurnValidatesClean:
    """A scripted native turn's emission sequence must validate clean
    through ``TurnEventValidator`` — exactly one ``turn_finished``,
    nothing after it (the W2 invariant)."""

    @pytest.mark.asyncio
    async def test_tool_turn_stream_validates_clean(self):
        from modex_agent.core.turn_validator import TurnEventValidator

        provider = MockStreamingProvider()
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
            if on_content_delta:
                await on_content_delta("Sunny in Beijing")
            return LLMResponse(content="Sunny in Beijing")

        provider.chat_stream = mock_chat_stream

        runtime = _make_runtime()
        context = AgentContext(
            system_prompt="You are a helpful assistant.",
            history=ListMessageHistory([{"role": "user", "content": "Hello"}]),
            tool_manager=_make_tool_manager(),
            max_iterations=3,
            identity=runtime.state.identity,
            runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )
        context.tool_manager.execute = AsyncMock(
            return_value=ToolResult.from_text("weather", "Sunny, 25C")
        )
        emitter = StreamingEmitter()
        agent = ReActAgent(provider=provider)

        result = await agent.run(context, emitter)

        assert result.stop_reason == StopReason.COMPLETED
        validator = TurnEventValidator()
        for event in emitter.get_events():
            validator.feed(event)
        assert validator.violations == []
        assert validator.finished is True
        terminals = emitter.get_events("turn_finished")
        assert len(terminals) == 1
        # The terminal is the LAST event of the turn.
        assert emitter.get_events()[-1] is terminals[0]

    @pytest.mark.asyncio
    async def test_error_turn_validates_clean(self):
        from modex_agent.core.turn_validator import TurnEventValidator

        provider = MockStreamingProvider()

        async def mock_chat_stream(*args, **kwargs):
            raise RuntimeError("model exploded")

        provider.chat_stream = mock_chat_stream

        runtime = _make_runtime()
        context = AgentContext(
            system_prompt="You are a helpful assistant.",
            history=ListMessageHistory([{"role": "user", "content": "Hello"}]),
            tool_manager=_make_tool_manager(),
            max_iterations=3,
            identity=runtime.state.identity,
            runtime=runtime,
            session=SessionInfo.from_str("test.agent"),
        )
        emitter = StreamingEmitter()
        agent = ReActAgent(provider=provider)

        result = await agent.run(context, emitter)

        assert result.stop_reason == StopReason.ERROR
        validator = TurnEventValidator()
        for event in emitter.get_events():
            validator.feed(event)
        assert validator.violations == []
        assert validator.finished is True
        terminals = emitter.get_events("turn_finished")
        assert len(terminals) == 1
