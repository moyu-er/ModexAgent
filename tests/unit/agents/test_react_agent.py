"""Tests for ReActAgent unified streaming and non-streaming paths.

验证 ReActAgent 的统一执行循环：
- 流式与非流式共享同一主循环
- _request_llm 根据 emitter.wants_streaming() 选择正确路径
- 内容通过 get_content()、推理通过 get_reasoning() 被 BufferingEmitter 收集
- 生命周期事件 (MODEL_OUTPUT, MODEL_REASONING) 仍然被分发
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.agents.react import ReActAgent, ReActEvent
from framework.core.agent import AgentContext
from framework.core.emitter import BufferingEmitter, StreamingAwareEmitter
from framework.core.provider import StreamingLLMProvider
from framework.core.tool_manager import ToolResult
from framework.core.types import LLMResponse, ToolCall
from framework.memory.history import ListMessageHistory


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


class StreamingEmitter(BufferingEmitter[ReActEvent]):
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
        return AgentContext(
            system_prompt="You are a helpful assistant.",
            history=ListMessageHistory([{"role": "user", "content": "Hello"}]),
            tool_manager=MagicMock(),
            max_iterations=3,
        )

    @pytest.fixture
    def emitter(self):
        return BufferingEmitter[ReActEvent]()

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
        context.tool_manager.execute = AsyncMock(return_value=ToolResult(tool_name="weather", result="Sunny, 25C"))
        agent = ReActAgent(provider=streaming_provider)

        result = await agent.run(context, streaming_emitter)

        context.tool_manager.execute.assert_called_once_with("weather", {"city": "Beijing"})
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

        class TrackingHook:
            async def after_llm_response(self, ctx, response):
                responses.append(response.content)

        streaming_provider._stream_content = ["Hello ", "World"]
        context.extensions["hooks"] = [TrackingHook()]
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
        context.tool_manager.execute = AsyncMock(return_value=ToolResult(tool_name="dummy", result="done"))
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

        class TrackingHook:
            async def after_llm_response(self, ctx, response):
                responses.append(response.content)

        async def mock_chat(*args, **kwargs):
            return LLMResponse(content="Complete response")

        non_streaming_provider.chat = mock_chat
        context.extensions["hooks"] = [TrackingHook()]
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
        context.tool_manager.execute = AsyncMock(return_value=ToolResult(tool_name="weather", result="Sunny, 25C"))
        agent = ReActAgent(provider=non_streaming_provider)

        result = await agent.run(context, emitter)

        context.tool_manager.execute.assert_called_once_with("weather", {"city": "Beijing"})
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
        # BufferingEmitter wants_streaming returns False by default
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
        context.tool_manager.execute = AsyncMock(return_value=ToolResult(tool_name="dummy", result="done"))
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
        return AgentContext(
            system_prompt="You are a helpful assistant.",
            history=ListMessageHistory([{"role": "user", "content": "Hello"}]),
            tool_manager=MagicMock(),
            max_iterations=3,
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
        context.tool_manager.execute = AsyncMock(return_value=ToolResult(tool_name="weather", result="Sunny"))

        class MockAdapter:
            supports_streaming = False
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

        class TrackingEmitter(BufferingEmitter[ReActEvent]):
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
        context.tool_manager.execute = AsyncMock(return_value=ToolResult(tool_name="weather", result="Sunny, 25C"))

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
    """崩溃恢复检查点测试。"""

    @pytest.fixture
    def streaming_provider(self):
        return MockStreamingProvider()

    @pytest.fixture
    def non_streaming_provider(self):
        return MockNonStreamingProvider()

    @pytest.fixture
    def context(self):
        return AgentContext(
            system_prompt="You are a helpful assistant.",
            history=ListMessageHistory([{"role": "user", "content": "Hello"}]),
            tool_manager=MagicMock(),
            max_iterations=3,
        )

    @pytest.fixture
    def emitter(self):
        return BufferingEmitter[ReActEvent]()

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
        context.tool_manager.execute = AsyncMock(return_value=ToolResult(tool_name="weather", result="Sunny, 25C"))

        saved: list[list[dict[str, Any]]] = []
        cleared: list[str] = []

        class _MockStore:
            async def save(self, cid, data):
                saved.append(list(data.get("messages", [])))
            async def clear(self, cid):
                cleared.append(cid)

        context.extensions["checkpoint_store"] = _MockStore()
        agent = ReActAgent(provider=streaming_provider)
        streaming_emitter = StreamingEmitter()

        result = await agent.run(context, streaming_emitter)

        # checkpoint 应在 assistant 后、每个 tool 后都同步保存
        assert len(saved) >= 3

        # 最终内容正确
        assert "Sunny in Beijing" in result.content

    @pytest.mark.asyncio
    async def test_checkpoint_cleared_on_final_output(self, non_streaming_provider, context, emitter):
        async def mock_chat(*args, **kwargs):
            return LLMResponse(content="Final answer")

        non_streaming_provider.chat = mock_chat

        cleared: list[str] = []

        class _MockStore:
            async def save(self, cid, data):
                pass
            async def clear(self, cid):
                cleared.append(cid)

        context.extensions["checkpoint_store"] = _MockStore()
        agent = ReActAgent(provider=non_streaming_provider)

        await agent.run(context, emitter)

        # checkpoint 应在结束时清除
        assert len(cleared) >= 1

    @pytest.mark.asyncio
    async def test_checkpoint_saved_on_error(self, non_streaming_provider, context, emitter):
        async def mock_chat(*args, **kwargs):
            raise ValueError("LLM failure")

        non_streaming_provider.chat = mock_chat

        saved: list[list[dict[str, Any]]] = []

        class _MockStore:
            async def save(self, cid, data):
                saved.append(list(data.get("messages", [])))
            async def clear(self, cid):
                pass

        context.extensions["checkpoint_store"] = _MockStore()
        agent = ReActAgent(provider=non_streaming_provider)

        result = await agent.run(context, emitter)

        assert result.stop_reason == "error"
        # 错误路径中也会保存 checkpoint（保留当前进度）
        assert len(saved) >= 1

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
        context.tool_manager.execute = AsyncMock(return_value=ToolResult(tool_name="weather", result="Sunny, 25C"))

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
