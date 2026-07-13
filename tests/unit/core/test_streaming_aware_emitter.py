"""Tests for StreamingAwareEmitter base class.

验证 StreamingAwareEmitter 的核心功能：
- 流式/非流式模式切换
- 内容缓冲和刷新
- 推理内容的处理
- 与 OutputAdapter 的集成
"""


import pytest

from modex_agent.adapters.platform import StreamingMode
from modex_agent.agents.react import ReActEvent
from modex_agent.core.emitter import AgentResult, StreamingAwareEmitter
from modex_agent.core.turn_events import (
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)
from modex_agent.core.types import ToolCall
from modex_agent.core.tool_manager import ToolResult


class MockOutputAdapter:
    """Mock OutputAdapter for testing."""

    def __init__(self, supports_streaming=False):
        self.streaming_mode = StreamingMode.NATIVE if supports_streaming else StreamingMode.PSEUDO
        self.send_delta_calls = []
        self.send_calls = []
        self.flush_deltas_calls = []

    async def send_delta(self, delta, session_id, metadata=None):
        self.send_delta_calls.append((delta, session_id, metadata))

    async def send(self, message, session_id):
        self.send_calls.append((message, session_id))

    async def flush_deltas(self, session_id):
        if self.send_delta_calls:
            from modex_agent.core.types import OutputMessage
            content = "".join([call[0] for call in self.send_delta_calls])
            await self.send(OutputMessage(content=content), session_id)
            self.send_delta_calls.clear()


class TestStreamingAwareEmitter:
    """StreamingAwareEmitter tests."""

    @pytest.fixture
    def mock_adapter(self):
        return MockOutputAdapter(supports_streaming=True)

    @pytest.fixture
    def mock_adapter_no_streaming(self):
        return MockOutputAdapter(supports_streaming=False)

    @pytest.fixture
    def emitter(self, mock_adapter):
        return StreamingAwareEmitter(
            output_adapter=mock_adapter,
            session_id="test_session",
        )

    @pytest.fixture
    def non_streaming_emitter(self, mock_adapter_no_streaming):
        return StreamingAwareEmitter(
            output_adapter=mock_adapter_no_streaming,
            session_id="test_session",
        )

    def test_is_true_streaming_with_support(self, mock_adapter):
        """Test is_true_streaming when adapter supports streaming."""
        emitter = StreamingAwareEmitter(
            output_adapter=mock_adapter,
            session_id="test",
        )
        assert emitter.is_true_streaming is True

    def test_is_true_streaming_without_support(self, mock_adapter_no_streaming):
        """Test is_true_streaming when adapter doesn't support streaming."""
        emitter = StreamingAwareEmitter(
            output_adapter=mock_adapter_no_streaming,
            session_id="test",
        )
        assert emitter.is_true_streaming is False

    def test_wants_streaming_native_returns_true(self, mock_adapter):
        """PSEUDO and NATIVE modes both use streaming LLM API."""
        emitter = StreamingAwareEmitter(
            output_adapter=mock_adapter,
            session_id="test",
        )
        assert emitter.wants_streaming() is True

    def test_wants_streaming_pseudo_returns_true(self, mock_adapter_no_streaming):
        """PSEUDO mode should use streaming LLM API (buffers deltas, flushes at end)."""
        emitter = StreamingAwareEmitter(
            output_adapter=mock_adapter_no_streaming,
            session_id="test",
        )
        assert emitter.wants_streaming() is True

    def test_wants_streaming_none_returns_false(self):
        """NONE mode should use non-streaming LLM API."""
        adapter = MockOutputAdapter(supports_streaming=False)
        adapter.streaming_mode = StreamingMode.NONE
        emitter = StreamingAwareEmitter(
            output_adapter=adapter,
            session_id="test",
        )
        assert emitter.wants_streaming() is False

    @pytest.mark.asyncio
    async def test_emit_delta_true_streaming(self, mock_adapter, emitter):
        """Test emit_delta in true streaming mode."""
        await emitter.emit_delta("Hello ")
        await emitter.emit_delta("World")

        # In true streaming mode, send_delta should be called immediately
        assert len(mock_adapter.send_delta_calls) == 2
        assert mock_adapter.send_delta_calls[0] == ("Hello ", "test_session", None)
        assert mock_adapter.send_delta_calls[1] == ("World", "test_session", None)

    @pytest.mark.asyncio
    async def test_emit_turn_event_forwards_only_canonical_text(self, mock_adapter, emitter):
        await emitter.emit_turn_event(TurnTextEvent(text="Hello"))
        await emitter.emit_turn_event(TurnReasoningEvent(text="thinking"))
        await emitter.emit_turn_event(
            TurnToolCallEvent(
                tool_name="bash", call_id="call-1", arguments={"command": "ls"}
            )
        )
        await emitter.emit_turn_event(
            TurnToolResultEvent(
                tool_name="bash", call_id="call-1", output="file.txt"
            )
        )

        assert mock_adapter.send_delta_calls == [("Hello", "test_session", None)]

    @pytest.mark.asyncio
    async def test_emit_delta_non_streaming(self, mock_adapter_no_streaming, non_streaming_emitter):
        """Test emit_delta in non-streaming mode."""
        await non_streaming_emitter.emit_delta("Hello ")
        await non_streaming_emitter.emit_delta("World")

        # In non-streaming mode, content should be buffered
        assert len(mock_adapter_no_streaming.send_delta_calls) == 0
        assert non_streaming_emitter._content_buffer == "Hello World"

    @pytest.mark.asyncio
    async def test_on_event_model_reasoning_buffers(self, emitter):
        """Test that _on_event with MODEL_REASONING buffers reasoning content."""
        await emitter.emit(ReActEvent.MODEL_REASONING, "Let me think... ")
        await emitter.emit(ReActEvent.MODEL_REASONING, "About this...")

        # Reasoning should be buffered but not sent
        assert emitter._reasoning_buffer == "Let me think... About this..."

    @pytest.mark.asyncio
    async def test_emit_complete_non_streaming(self, mock_adapter_no_streaming, non_streaming_emitter):
        """Test emit_complete flushes buffer in non-streaming mode."""
        # Add some content
        await non_streaming_emitter.emit_delta("Hello World")
        await non_streaming_emitter.emit(ReActEvent.MODEL_REASONING, "Some reasoning")

        # Complete
        result = AgentResult(content="Hello World", reasoning="Some reasoning")
        await non_streaming_emitter.emit_complete(result)

        # Buffer should be flushed and cleared
        assert len(mock_adapter_no_streaming.send_calls) == 1
        message, session_id = mock_adapter_no_streaming.send_calls[0]
        assert message.content == "Hello World"
        assert message.metadata.get("reasoning") == "Some reasoning"
        assert session_id == "test_session"

        # Buffers should be cleared
        assert non_streaming_emitter._content_buffer == ""
        assert non_streaming_emitter._reasoning_buffer == ""

    @pytest.mark.asyncio
    async def test_emit_complete_streaming(self, mock_adapter, emitter):
        """Test emit_complete in streaming mode just clears buffers."""
        # Add some content (already sent in streaming mode)
        await emitter.emit_delta("Hello")
        assert len(mock_adapter.send_delta_calls) == 1

        # Complete
        result = AgentResult(content="Hello")
        await emitter.emit_complete(result)

        # In streaming mode, no additional send should happen
        assert len(mock_adapter.send_calls) == 0
        assert emitter._content_buffer == ""

    @pytest.mark.asyncio
    async def test_emit_error(self, mock_adapter, emitter):
        """Test emit_error sends error message."""
        await emitter.emit_error("Something went wrong")

        assert len(mock_adapter.send_calls) == 1
        message, session_id = mock_adapter.send_calls[0]
        assert "Error: Something went wrong" in message.content
        assert session_id == "test_session"

    @pytest.mark.asyncio
    async def test_emit_delta_empty_string(self, emitter):
        """Test emit_delta with empty string is ignored."""
        await emitter.emit_delta("")
        await emitter.emit_delta("Hello")
        await emitter.emit_delta("")

        # Only "Hello" should be processed
        # In true streaming mode, send_delta should be called once
        # (since mock_adapter supports streaming)

    @pytest.mark.asyncio
    async def test_emit_delta_none(self, emitter):
        """Test emit_delta with None is handled gracefully."""
        # Should not raise
        await emitter.emit_delta(None)

    @pytest.mark.asyncio
    async def test_on_event_tool_call_start(self, emitter):
        """Test _on_event with TOOL_CALL_START default implementation (does nothing to adapter)."""
        tool_call = ToolCall(tool_name="test_tool", arguments={"arg": "value"})
        # Should not raise, default implementation does nothing to adapter
        await emitter.emit(ReActEvent.TOOL_CALL_START, tool_call)

        # No calls to adapter
        assert len(emitter.output_adapter.send_delta_calls) == 0
        assert len(emitter.output_adapter.send_calls) == 0

    @pytest.mark.asyncio
    async def test_on_event_tool_call_end(self, emitter):
        """Test _on_event with TOOL_CALL_END default implementation (does nothing to adapter)."""
        tool_call = ToolCall(tool_name="test_tool", arguments={})
        result = ToolResult(tool_name="test_tool", result="success")
        # Should not raise, default implementation does nothing to adapter
        await emitter.emit(ReActEvent.TOOL_CALL_END, (tool_call, result))

        # No calls to adapter
        assert len(emitter.output_adapter.send_delta_calls) == 0
        assert len(emitter.output_adapter.send_calls) == 0


class TestStreamingAwareEmitterWithEvents:
    """Test StreamingAwareEmitter with actual ReActEvent dispatching."""

    @pytest.fixture
    def mock_adapter(self):
        return MockOutputAdapter(supports_streaming=False)

    @pytest.fixture
    def emitter(self, mock_adapter):
        return StreamingAwareEmitter(
            output_adapter=mock_adapter,
            session_id="test_session",
        )

    @pytest.mark.asyncio
    async def test_handle_model_output_event(self, mock_adapter, emitter):
        """Test that emit_delta buffers content in pseudo-streaming mode."""
        await emitter.emit_delta("Hello ")
        await emitter.emit_delta("World")

        # Content should be buffered
        assert emitter._content_buffer == "Hello World"

    @pytest.mark.asyncio
    async def test_handle_model_reasoning_event(self, mock_adapter, emitter):
        """Test that _on_event with MODEL_REASONING buffers reasoning in pseudo-streaming mode."""
        await emitter.emit(ReActEvent.MODEL_REASONING, "Let me think...")

        # Reasoning should be buffered
        assert emitter._reasoning_buffer == "Let me think..."

    @pytest.mark.asyncio
    async def test_emit_stream_end_clears_buffer(self, mock_adapter, emitter):
        """Regression: emit_stream_end should flush and clear the buffer."""
        await emitter.emit_delta("First iteration")
        await emitter.emit_stream_end(resuming=True)

        # Buffer should be flushed and cleared
        assert len(mock_adapter.send_calls) == 1
        assert mock_adapter.send_calls[0][0].content == "First iteration"
        assert emitter._content_buffer == ""

        # Second iteration should not accumulate previous content
        await emitter.emit_delta("Second iteration")
        await emitter.emit_stream_end(resuming=True)

        assert len(mock_adapter.send_calls) == 2
        assert mock_adapter.send_calls[1][0].content == "Second iteration"
        assert emitter._content_buffer == ""
