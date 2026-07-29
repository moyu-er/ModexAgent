"""Tests for QQBotEmitter business logic.

验证 QQBotEmitter 的业务处理逻辑：
- 内容发送给用户
- 推理内容只记日志
- 工具调用被忽略
- 与 QQOutputAdapter 的集成
"""

import logging

import pytest

from modex_agent.adapters.platform import StreamingMode
from modex_agent.agents.react import ReActEvent
from modex_agent.core.emitter import AgentResult
from modex_agent.core.types import ToolCall
from modex_agent.core.tool_manager import ToolResult


class MockOutputAdapter:
    """Mock OutputAdapter for testing."""

    def __init__(self):
        self.streaming_mode = StreamingMode.PSEUDO
        self.send_delta_calls = []
        self.send_calls = []

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


class TestQQBotEmitter:
    """QQBotEmitter tests."""

    @pytest.fixture
    def mock_adapter(self):
        return MockOutputAdapter()

    @pytest.fixture
    def emitter(self, mock_adapter):
        """Create a QQBotEmitter instance."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "examples" / "bot_project"))
        from bot.adapters.qq import QQBotEmitter
        return QQBotEmitter(
            output_adapter=mock_adapter,
            session_id="test_qq_session",
        )

    @pytest.mark.asyncio
    async def test_emit_delta_buffers_in_pseudo_streaming(self, mock_adapter, emitter):
        """Test that emit_delta buffers content in pseudo-streaming mode (QQ)."""
        await emitter.emit_delta("Hello ")
        await emitter.emit_delta("QQ User!")

        # In pseudo-streaming mode, content is buffered internally, not sent immediately
        assert len(mock_adapter.send_delta_calls) == 0
        assert emitter._content_buffer == "Hello QQ User!"

    @pytest.mark.asyncio
    async def test_model_reasoning_logs_only(self, mock_adapter, emitter, caplog):
        """Test that model_reasoning only logs, doesn't send to user."""
        with caplog.at_level(logging.INFO):
            await emitter.emit(ReActEvent.MODEL_REASONING, "Let me think...")
            await emitter.emit(ReActEvent.MODEL_REASONING, "This is my reasoning")

        # Should log reasoning
        assert "[Reasoning]" in caplog.text
        assert "Let me think..." in caplog.text

        # Should NOT send to adapter
        assert len(mock_adapter.send_delta_calls) == 0

    @pytest.mark.asyncio
    async def test_tool_call_start_ignored(self, mock_adapter, emitter):
        """Test that tool_call_start is ignored (not sent to user)."""
        tool_call = ToolCall(tool_name="weather", arguments={"city": "Beijing"})

        # Should not raise or send anything
        await emitter.emit(ReActEvent.TOOL_CALL_START, tool_call)

        # No calls to adapter
        assert len(mock_adapter.send_delta_calls) == 0
        assert len(mock_adapter.send_calls) == 0

    @pytest.mark.asyncio
    async def test_tool_call_end_ignored(self, mock_adapter, emitter):
        """Test that tool_call_end is ignored (not sent to user)."""
        tool_call = ToolCall(tool_name="weather", arguments={})
        result = ToolResult.from_text("weather", "Sunny, 25C")

        # Should not raise or send anything
        await emitter.emit(ReActEvent.TOOL_CALL_END, (tool_call, result))

        # No calls to adapter
        assert len(mock_adapter.send_delta_calls) == 0
        assert len(mock_adapter.send_calls) == 0

    @pytest.mark.asyncio
    async def test_emit_complete_flushes_buffer(self, mock_adapter, emitter):
        """Test that emit_complete flushes buffered content via send()."""
        await emitter.emit_delta("Hello ")
        await emitter.emit_delta("World")

        result = AgentResult(content="Hello World", reasoning="Some reasoning")
        await emitter.emit_complete(result)

        # In pseudo-streaming mode, flush goes through send()
        assert len(mock_adapter.send_calls) == 1
        assert mock_adapter.send_calls[0][0].content == "Hello World"

    @pytest.mark.asyncio
    async def test_business_logic_demonstration(self, mock_adapter, emitter, caplog):
        """Test demonstrating the complete QQ Bot business logic."""

        # 1. Model generates content and reasoning
        with caplog.at_level(logging.INFO):
            await emitter.emit(ReActEvent.MODEL_REASONING, "Step 1: Analyzing question...")
            await emitter.emit_delta("The answer is ")
            await emitter.emit(ReActEvent.MODEL_REASONING, "Step 2: Computing...")
            await emitter.emit_delta("42")

        # 2. Content should be buffered internally (pseudo-streaming)
        assert len(mock_adapter.send_delta_calls) == 0
        assert emitter._content_buffer == "The answer is 42"

        # 3. Reasoning should be logged, not sent
        assert "Step 1: Analyzing question..." in caplog.text
        assert "Step 2: Computing..." in caplog.text

        # 4. Tool calls should be ignored
        tool_call = ToolCall(tool_name="calculator", arguments={"expr": "20+22"})
        await emitter.emit(ReActEvent.TOOL_CALL_START, tool_call)
        assert len(mock_adapter.send_delta_calls) == 0

        # 5. Complete the response - flushes via send()
        result = AgentResult(content="The answer is 42", reasoning="Computed 20+22")
        await emitter.emit_complete(result)

        assert len(mock_adapter.send_calls) == 1
        assert mock_adapter.send_calls[0][0].content == "The answer is 42"
