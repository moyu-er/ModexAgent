"""Tests for HTTPOutputAdapter.

验证 HTTPOutputAdapter 的 SSE 流式输出行为：
- send_delta 生成 SSE delta 事件
- flush_deltas 生成 SSE flush 事件
- send 生成 SSE message 事件（包含 reasoning）
- supports_streaming = True
- iter_sse 生成器可消费事件
"""

import pytest
import json

from framework.pipeline.adapters import HTTPOutputAdapter
from framework.core.types import OutputMessage


class TestHTTPOutputAdapter:
    """HTTPOutputAdapter tests."""

    @pytest.fixture
    def adapter(self):
        return HTTPOutputAdapter()

    @pytest.mark.asyncio
    async def test_send_delta_creates_sse_event(self, adapter):
        """Test that send_delta puts an SSE delta event into the queue."""
        await adapter.send_delta("Hello ", "session_1")
        await adapter.send_delta("World", "session_1")

        event1 = await adapter.sse_queue.get()
        event2 = await adapter.sse_queue.get()

        assert event1.startswith("data: ")
        data1 = json.loads(event1.replace("data: ", "", 1).strip())
        assert data1["type"] == "delta"
        assert data1["session_id"] == "session_1"
        assert data1["content"] == "Hello "

        data2 = json.loads(event2.replace("data: ", "", 1).strip())
        assert data2["content"] == "World"

    @pytest.mark.asyncio
    async def test_flush_deltas_creates_flush_event(self, adapter):
        """Test that flush_deltas puts an SSE flush event into the queue."""
        await adapter.flush_deltas("session_1")

        event = await adapter.sse_queue.get()
        data = json.loads(event.replace("data: ", "", 1).strip())
        assert data["type"] == "flush"
        assert data["session_id"] == "session_1"

    @pytest.mark.asyncio
    async def test_send_creates_message_event(self, adapter):
        """Test that send puts an SSE message event into the queue."""
        await adapter.send(
            OutputMessage(content="Complete message", reasoning="Thinking..."),
            "session_1",
        )

        event = await adapter.sse_queue.get()
        data = json.loads(event.replace("data: ", "", 1).strip())
        assert data["type"] == "message"
        assert data["session_id"] == "session_1"
        assert data["content"] == "Complete message"
        assert data["reasoning"] == "Thinking..."
        assert data["message_type"] == "text"

    @pytest.mark.asyncio
    async def test_send_delta_with_metadata(self, adapter):
        """Test that send_delta passes metadata through."""
        await adapter.send_delta("chunk", "session_1", metadata={"index": 1})

        event = await adapter.sse_queue.get()
        data = json.loads(event.replace("data: ", "", 1).strip())
        assert data["metadata"]["index"] == 1

    def test_supports_streaming_true(self, adapter):
        """Test that HTTP adapter reports true streaming support."""
        assert adapter.supports_streaming is True

    @pytest.mark.asyncio
    async def test_iter_sse_generator(self, adapter):
        """Test iter_sse async generator yields queued events."""
        await adapter.send_delta("chunk", "session_1")
        await adapter.sse_queue.put(None)  # Signal end

        gen = adapter.iter_sse()
        event = await gen.__anext__()
        assert "chunk" in event

        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    @pytest.mark.asyncio
    async def test_send_without_reasoning(self, adapter):
        """Test that message events without reasoning don't include the field."""
        await adapter.send(OutputMessage(content="Just content"), "session_1")

        event = await adapter.sse_queue.get()
        data = json.loads(event.replace("data: ", "", 1).strip())
        assert "reasoning" not in data
