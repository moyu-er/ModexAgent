"""Tests for QQOutputAdapter send_delta and flush_deltas.

验证 QQOutputAdapter 的流式相关功能：
- send_delta 缓冲内容
- flush_deltas 合并并发送
- supports_streaming = False
- 内容清理（移除 think 标签）
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.pipeline.adapters import OutputMessage


class TestQQOutputAdapter:
    """QQOutputAdapter tests."""

    @pytest.fixture
    def mock_qq_input(self):
        """Create a mock QQInputAdapter."""
        mock = MagicMock()
        mock._client = MagicMock()
        mock._client.api = MagicMock()
        mock._client.api.post_c2c_message = AsyncMock()
        mock._client.api.post_group_message = AsyncMock()
        mock.last_input_metadata = {}
        return mock

    @pytest.fixture
    def adapter(self, mock_qq_input):
        """Import and create QQOutputAdapter."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "examples" / "bot_project"))
        from bot.adapters.qq import QQOutputAdapter
        return QQOutputAdapter(mock_qq_input)

    @pytest.mark.asyncio
    async def test_send_delta_buffers_content(self, adapter):
        """Test that send_delta buffers content."""
        await adapter.send_delta("Hello ", "session_1")
        await adapter.send_delta("World", "session_1")

        # Content should be buffered, not sent yet
        assert "session_1" in adapter._delta_buffers
        assert adapter._delta_buffers["session_1"] == ["Hello ", "World"]

        # post_c2c_message should not be called yet
        adapter._qq_input._client.api.post_c2c_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_delta_per_session(self, adapter):
        """Test that send_delta maintains separate buffers per session."""
        await adapter.send_delta("Hello ", "session_1")
        await adapter.send_delta("Bonjour ", "session_2")
        await adapter.send_delta("World", "session_1")
        await adapter.send_delta("Monde", "session_2")

        assert adapter._delta_buffers["session_1"] == ["Hello ", "World"]
        assert adapter._delta_buffers["session_2"] == ["Bonjour ", "Monde"]

    @pytest.mark.asyncio
    async def test_send_delta_empty_string(self, adapter):
        """Test that empty strings are handled gracefully."""
        await adapter.send_delta("", "session_1")
        # Should not create buffer entry for empty string

        await adapter.send_delta("Hello", "session_1")
        assert adapter._delta_buffers["session_1"] == ["Hello"]

    @pytest.mark.asyncio
    async def test_flush_deltas_sends_content(self, adapter):
        """Test that flush_deltas sends buffered content."""
        await adapter.send_delta("Hello ", "session_1")
        await adapter.send_delta("World", "session_1")
        await adapter.flush_deltas("session_1")

        # Content should be sent
        adapter._qq_input._client.api.post_c2c_message.assert_called_once()
        call_args = adapter._qq_input._client.api.post_c2c_message.call_args
        assert call_args.kwargs['openid'] == "session_1"
        assert call_args.kwargs['content'] == "Hello World"

    @pytest.mark.asyncio
    async def test_flush_deltas_clears_buffer(self, adapter):
        """Test that flush_deltas clears the buffer."""
        await adapter.send_delta("Hello", "session_1")
        await adapter.flush_deltas("session_1")

        # Buffer should be cleared
        assert "session_1" not in adapter._delta_buffers

    @pytest.mark.asyncio
    async def test_flush_deltas_empty_buffer(self, adapter):
        """Test flush_deltas with no buffered content."""
        # Should not raise or send anything
        await adapter.flush_deltas("nonexistent_session")
        adapter._qq_input._client.api.post_c2c_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_flush_deltas_per_session(self, adapter):
        """Test that flush_deltas only sends for specified session."""
        await adapter.send_delta("Hello ", "session_1")
        await adapter.send_delta("Bonjour ", "session_2")
        await adapter.send_delta("World", "session_1")

        # Flush only session_1
        await adapter.flush_deltas("session_1")

        # Only session_1 should be sent
        assert adapter._qq_input._client.api.post_c2c_message.call_count == 1
        assert "session_2" in adapter._delta_buffers

    def test_supports_streaming(self, adapter):
        """Test that supports_streaming returns False."""
        assert adapter.supports_streaming is False

    @pytest.mark.asyncio
    async def test_send_with_content_cleaning(self, adapter):
        """Test that send applies content filters (whitespace cleanup)."""
        message = OutputMessage(content="  Hello  ")
        await adapter.send(message, "session_1")

        call_args = adapter._qq_input._client.api.post_c2c_message.call_args
        assert "Hello" in call_args.kwargs['content']

    @pytest.mark.asyncio
    async def test_send_empty_content(self, adapter):
        """Test that empty content is not sent."""
        message = OutputMessage(content="")
        await adapter.send(message, "session_1")

        # Should not send
        adapter._qq_input._client.api.post_c2c_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_no_content_message(self, adapter):
        """Test that '(无回复内容)' is not sent."""
        message = OutputMessage(content="（无回复内容）")
        await adapter.send(message, "session_1")

        # Should not send
        adapter._qq_input._client.api.post_c2c_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_stream_integration(self, adapter):
        """Test send_stream method."""
        async def content_generator():
            yield "Hello "
            yield "World"
            yield "!"

        await adapter.send_stream(content_generator(), "session_1")

        # All content should be sent
        call_args = adapter._qq_input._client.api.post_c2c_message.call_args
        assert call_args.kwargs['content'] == "Hello World!"

    @pytest.mark.asyncio
    async def test_send_group_message_routing(self, adapter, mock_qq_input):
        """Test that messages route to post_group_message when group_openid is present."""
        mock_raw_msg = MagicMock()
        mock_raw_msg.group_openid = "group_123"
        mock_qq_input.last_input_metadata = {"raw_message": mock_raw_msg}

        message = OutputMessage(content="Hello Group!")
        await adapter.send(message, "session_1")

        mock_qq_input._client.api.post_group_message.assert_called_once()
        call_args = mock_qq_input._client.api.post_group_message.call_args
        assert call_args.kwargs['group_openid'] == "group_123"
        assert call_args.kwargs['content'] == "Hello Group!"
        mock_qq_input._client.api.post_c2c_message.assert_not_called()
