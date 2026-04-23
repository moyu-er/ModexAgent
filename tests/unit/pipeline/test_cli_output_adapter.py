"""Tests for CLIOutputAdapter.

验证 CLIOutputAdapter 的真流式输出行为：
- send_delta 立即打印到 stdout
- flush_deltas 打印换行
- send 打印完整消息
- supports_streaming = True
"""

import pytest

from framework.pipeline.adapters import CLIOutputAdapter
from framework.core.types import OutputMessage


class TestCLIOutputAdapter:
    """CLIOutputAdapter tests."""

    @pytest.fixture
    def adapter(self):
        return CLIOutputAdapter()

    @pytest.mark.asyncio
    async def test_send_delta_prints_immediately(self, adapter, capsys):
        """Test that send_delta prints each delta immediately."""
        await adapter.send_delta("Hello ", "session_1")
        await adapter.send_delta("World", "session_1")

        captured = capsys.readouterr()
        assert captured.out == "Hello World"

    @pytest.mark.asyncio
    async def test_flush_deltas_prints_newline(self, adapter, capsys):
        """Test that flush_deltas prints a newline."""
        await adapter.send_delta("Hello", "session_1")
        await adapter.flush_deltas("session_1")

        captured = capsys.readouterr()
        assert captured.out == "Hello\n"

    @pytest.mark.asyncio
    async def test_send_prints_full_message(self, adapter, capsys):
        """Test that send prints a complete message."""
        await adapter.send(OutputMessage(content="Complete message"), "session_1")

        captured = capsys.readouterr()
        assert captured.out == "Complete message\n"

    @pytest.mark.asyncio
    async def test_send_with_reasoning(self, adapter, capsys):
        """Test that send handles reasoning field."""
        await adapter.send(
            OutputMessage(content="Answer", reasoning="Thinking..."),
            "session_1"
        )

        captured = capsys.readouterr()
        assert captured.out == "Answer\n"

    def test_supports_streaming_true(self, adapter):
        """Test that CLI adapter reports true streaming support."""
        assert adapter.supports_streaming is True

    @pytest.mark.asyncio
    async def test_send_delta_ignores_empty_delta(self, adapter, capsys):
        """Test that empty deltas are ignored."""
        await adapter.send_delta("", "session_1")
        await adapter.send_delta(None, "session_1")

        captured = capsys.readouterr()
        assert captured.out == ""

    @pytest.mark.asyncio
    async def test_custom_prefix_suffix(self, capsys):
        """Test custom prefix and suffix configuration."""
        adapter = CLIOutputAdapter(prefix=">>> ", suffix="\r\n")
        await adapter.send(OutputMessage(content="Hi"), "session_1")

        captured = capsys.readouterr()
        assert captured.out == ">>> Hi\r\n"

    @pytest.mark.asyncio
    async def test_send_empty_content(self, adapter, capsys):
        """Test that empty content messages produce no output."""
        await adapter.send(OutputMessage(content=""), "session_1")

        captured = capsys.readouterr()
        assert captured.out == ""

