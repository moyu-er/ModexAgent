"""WebSocket input/output adapters for browser-based UI."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from framework.adapters.platform import StreamingMode
from framework.core.types import InputMessage, OutputMessage
from framework.pipeline.adapters import InputAdapter, OutputAdapter

# ── Constants ──────────────────────────────────────────────────────────────

WEBSOCKET_CHANNEL: str = "websocket"


class WebSocketInputAdapter(InputAdapter):
    """Input adapter that receives messages from WebSocket connections.

    Manages per-session delta queues so the output adapter can push
    streaming deltas directly to the correct WebSocket connection.

    Lifetime is managed by the WebUI server, so start()/stop() are no-ops.
    """

    def __init__(self) -> None:
        super().__init__()
        self._message_queue: asyncio.Queue[InputMessage] = asyncio.Queue()
        self._connections: dict[str, object] = {}
        self._delta_queues: dict[str, asyncio.Queue[str]] = {}

    @property
    def name(self) -> str:
        return WEBSOCKET_CHANNEL

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def receive(self) -> AsyncIterator[InputMessage]:
        while True:
            msg = await self._message_queue.get()
            yield msg

    def register_connection(self, session_id: str, ws: object) -> None:
        """Register a WebSocket connection for a session and create its delta queue."""
        self._connections[session_id] = ws
        self._delta_queues[session_id] = asyncio.Queue()

    def unregister_connection(self, session_id: str) -> None:
        """Remove a WebSocket connection and its delta queue."""
        self._connections.pop(session_id, None)
        self._delta_queues.pop(session_id, None)

    def get_delta_queue(self, session_id: str) -> asyncio.Queue[str] | None:
        """Return the delta queue for a session, or None if not registered."""
        return self._delta_queues.get(session_id)

    def enqueue_user_message(self, session_id: str, content: str) -> None:
        """Enqueue a user message to be consumed by receive()."""
        msg = InputMessage(content=content, session_id=session_id, channel=WEBSOCKET_CHANNEL)
        self._message_queue.put_nowait(msg)


class WebSocketOutputAdapter(OutputAdapter):
    """Output adapter that sends streaming deltas to WebSocket connections.

    Each delta is pushed immediately into the per-session queue managed
    by WebSocketInputAdapter, so the WebUI server can forward it to the
    correct browser client.
    """

    def __init__(self, input_adapter: WebSocketInputAdapter) -> None:
        self._input = input_adapter

    @property
    def name(self) -> str:
        return WEBSOCKET_CHANNEL

    @property
    def streaming_mode(self) -> StreamingMode:
        return StreamingMode.NATIVE

    async def send(self, message: OutputMessage, session_id: str) -> None:
        content = message.content or ""
        await self.send_delta(content, session_id)

    async def send_delta(
        self, delta: str, session_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        q = self._input.get_delta_queue(session_id)
        if q is not None:
            q.put_nowait(delta)

    async def flush_deltas(self, session_id: str) -> None:
        pass
