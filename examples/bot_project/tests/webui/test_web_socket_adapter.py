"""Tests for WebSocket input/output adapters."""

from __future__ import annotations

import pytest
from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter

from modex_agent.adapters.platform import StreamingMode


@pytest.mark.asyncio
async def test_input_adapter_name() -> None:
    adapter = WebSocketInputAdapter()
    assert adapter.name == "websocket"


@pytest.mark.asyncio
async def test_output_adapter_streaming_mode() -> None:
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    assert output_adapter.streaming_mode == StreamingMode.NATIVE


@pytest.mark.asyncio
async def test_send_delta_routes_to_session() -> None:
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_connection("sess1", None)
    await output_adapter.send_delta("hello", "sess1")
    q = input_adapter._delta_queues.get("sess1")
    assert q is not None
    envelope = q.get_nowait()
    assert envelope.event_type == "content"
    assert envelope.payload == {"text": "hello"}


@pytest.mark.asyncio
async def test_enqueue_user_message_and_receive() -> None:
    """Verify enqueue_user_message → receive yields the InputMessage."""
    adapter = WebSocketInputAdapter()
    adapter.enqueue_user_message("sess1", "hello world")
    gen = adapter.receive()
    msg = await gen.__anext__()
    assert msg.content == "hello world"
    assert msg.session.agent_name == "main"
    assert msg.channel == "websocket"


@pytest.mark.asyncio
async def test_unregister_connection_cleanup() -> None:
    """Verify unregister removes both connection and delta queue."""
    adapter = WebSocketInputAdapter()
    adapter.register_connection("sess1", None)
    assert "sess1" in adapter._delta_queues
    adapter.unregister_connection("sess1")
    assert "sess1" not in adapter._connections
    assert "sess1" not in adapter._delta_queues


@pytest.mark.asyncio
async def test_send_delta_to_unregistered_session_is_noop() -> None:
    """Sending a delta to an unregistered session should silently no-op."""
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    # Should not raise — unregistered session, no queue
    await output_adapter.send_delta("ghost", "nonexistent")


@pytest.mark.asyncio
async def test_delta_queue_drops_when_full() -> None:
    """A full delta queue must drop new deltas instead of growing unbounded.

    Deltas are transient UI refresh; dropping them under backpressure protects
    memory when a client disconnects or lags, without crashing the agent.
    """
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_connection("sess1", None)
    q = input_adapter.get_delta_queue("sess1")
    assert q is not None
    capacity = q.maxsize

    # Fill the queue exactly to capacity.
    for i in range(capacity):
        await output_adapter.send_delta(f"chunk-{i}", "sess1")
    assert q.qsize() == capacity

    # One more beyond capacity: must not raise and must not grow the queue.
    await output_adapter.send_delta("overflow", "sess1")
    assert q.qsize() == capacity

