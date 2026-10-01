"""Tests for QQBotEmitter business logic.

验证 QQBotEmitter 的业务处理逻辑：
- 内容按投递策略缓冲/发送给用户
- 推理内容只记日志
- 工具调用被忽略（仅日志）
- 与 QQOutputAdapter 的集成
"""

import logging
import sys
from pathlib import Path

import pytest

from modex_agent.adapters.platform import StreamingMode
from modex_agent.core.emitter import AgentResult, turn_finished_event
from modex_agent.core.message import ToolCall
from modex_agent.core.tool_manager import ToolResult
from modex_agent.core.turn_events import (
    IterationFinishedEvent,
    TurnErroredEvent,
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "examples" / "bot_project"))
from bot.adapters.qq import QQBotEmitter  # noqa: E402


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
            from modex_agent.messaging.models import OutputMessage
            content = "".join([call[0] for call in self.send_delta_calls])
            await self.send(OutputMessage(content=content), session_id)
            self.send_delta_calls.clear()

    @property
    def name(self) -> str:
        return "mock"


class TestQQBotEmitter:
    """QQBotEmitter tests."""

    @pytest.fixture
    def mock_adapter(self):
        return MockOutputAdapter()

    @pytest.fixture
    def emitter(self, mock_adapter):
        """Create a QQBotEmitter instance (default gate — every kind)."""
        return QQBotEmitter(
            output_adapter=mock_adapter,
            session_id="test_qq_session",
        )

    @pytest.mark.asyncio
    async def test_text_buffers_in_segment_policy(self, mock_adapter, emitter):
        """Text buffers under the SEGMENT policy (QQ adapter is PSEUDO)."""
        await emitter.emit(TurnTextEvent(text="Hello "))
        await emitter.emit(TurnTextEvent(text="QQ User!"))

        assert len(mock_adapter.send_delta_calls) == 0
        assert emitter._content_buffer == "Hello QQ User!"

    @pytest.mark.asyncio
    async def test_reasoning_logs_only(self, mock_adapter, emitter, caplog):
        """Reasoning only logs, never reaches the user."""
        with caplog.at_level(logging.INFO):
            await emitter.emit(TurnReasoningEvent(text="Let me think..."))
            await emitter.emit(TurnReasoningEvent(text="This is my reasoning"))

        # Should log reasoning
        assert "[Reasoning]" in caplog.text
        assert "Let me think..." in caplog.text

        # Should NOT send to the adapter
        assert len(mock_adapter.send_delta_calls) == 0
        assert mock_adapter.send_calls == []

    @pytest.mark.asyncio
    async def test_tool_call_ignored(self, mock_adapter, emitter, caplog):
        """Tool calls are logged, not sent to the user."""
        with caplog.at_level(logging.INFO):
            await emitter.emit(
                TurnToolCallEvent(
                    tool_name="weather", call_id="call-1", arguments={"city": "Beijing"}
                )
            )

        assert "[Tool Call]" in caplog.text
        # No calls to adapter
        assert len(mock_adapter.send_delta_calls) == 0
        assert len(mock_adapter.send_calls) == 0

    @pytest.mark.asyncio
    async def test_tool_result_ignored(self, mock_adapter, emitter):
        """Tool results are logged, not sent to the user."""
        await emitter.emit(
            TurnToolResultEvent(
                tool_name="weather", call_id="call-1", output="Sunny, 25C"
            )
        )

        # No calls to adapter
        assert len(mock_adapter.send_delta_calls) == 0
        assert len(mock_adapter.send_calls) == 0

    @pytest.mark.asyncio
    async def test_turn_finished_flushes_buffer(self, mock_adapter, emitter):
        """turn_finished flushes buffered content via send()."""
        await emitter.emit(TurnTextEvent(text="Hello "))
        await emitter.emit(TurnTextEvent(text="World"))

        await emitter.emit(turn_finished_event(AgentResult(content="Hello World")))
        # In segment mode, flush goes through send()
        assert len(mock_adapter.send_calls) == 1
        assert mock_adapter.send_calls[0][0].content == "Hello World"

    @pytest.mark.asyncio
    async def test_error_sends_error_message(self, mock_adapter, emitter):
        await emitter.emit(TurnErroredEvent(message="boom"))
        assert len(mock_adapter.send_calls) == 1
        assert "Error: boom" in mock_adapter.send_calls[0][0].content

    @pytest.mark.asyncio
    async def test_business_logic_demonstration(self, mock_adapter, emitter, caplog):
        """Test demonstrating the complete QQ Bot business logic."""

        # 1. Model generates content and reasoning
        with caplog.at_level(logging.INFO):
            await emitter.emit(TurnReasoningEvent(text="Step 1: Analyzing question..."))
            await emitter.emit(TurnTextEvent(text="The answer is "))
            await emitter.emit(TurnReasoningEvent(text="Step 2: Computing..."))
            await emitter.emit(TurnTextEvent(text="42"))

        # 2. Content should be buffered internally (segment policy)
        assert len(mock_adapter.send_delta_calls) == 0
        assert emitter._content_buffer == "The answer is 42"

        # 3. Reasoning should be logged, not sent
        assert "Step 1: Analyzing question..." in caplog.text
        assert "Step 2: Computing..." in caplog.text

        # 4. Tool calls should be ignored
        await emitter.emit(
            TurnToolCallEvent(
                tool_name="calculator", call_id="call-2", arguments={"expr": "20+22"}
            )
        )
        assert len(mock_adapter.send_delta_calls) == 0

        # 5. Segment boundary flushes via send()
        await emitter.emit(IterationFinishedEvent(iteration=1, has_tool_calls=True))
        assert len(mock_adapter.send_calls) == 1
        assert mock_adapter.send_calls[0][0].content == "The answer is 42"

    @pytest.mark.asyncio
    async def test_minimal_gate_drops_reasoning(self, mock_adapter):
        """The production IM gate excludes reasoning (as before)."""
        from bot.adapters.channels import im_channel_gate

        gated = QQBotEmitter(
            output_adapter=mock_adapter,
            session_id="test_qq_session",
            gate=im_channel_gate(),
        )
        await gated.emit(TurnReasoningEvent(text="hidden"))
        assert gated._reasoning_buffer == ""
