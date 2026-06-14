"""WebSocket input/output adapters for browser-based UI."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from bot.webui.events import DeltaEnvelope
from framework.adapters.platform import StreamingMode
from framework.core.types import InputMessage, OutputMessage
from framework.pipeline.adapters import InputAdapter, OutputAdapter

# ── Constants ──────────────────────────────────────────────────────────────

WEBSOCKET_CHANNEL: str = "websocket"


def _agent_of(session_id: str) -> str:
    """Return the agent segment (2nd) of a full session id, default ``main``."""
    parts = session_id.split(".", 2)
    return parts[1] if len(parts) >= 2 else "main"


class WebSocketInputAdapter(InputAdapter):
    """Input adapter that receives messages from WebSocket connections.

    Manages per-session delta queues so the output adapter can push
    streaming deltas directly to the correct WebSocket connection.

    Each queue holds structured :class:`DeltaEnvelope` objects (not flat
    strings), serialized once at the WebSocket forwarding boundary.

    Lifetime is managed by the WebUI server, so start()/stop() are no-ops.
    """

    def __init__(self) -> None:
        super().__init__()
        self._message_queue: asyncio.Queue[InputMessage] = asyncio.Queue()
        self._connections: dict[str, object] = {}
        self._delta_queues: dict[str, asyncio.Queue[DeltaEnvelope]] = {}

    @property
    def name(self) -> str:
        return WEBSOCKET_CHANNEL

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def configure_input_pipeline(
        self,
        pipeline,
        ctx,
        output,
    ) -> None:
        """WebSocket pipeline is held by the server — no-op here."""

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

    def get_delta_queue(self, session_id: str) -> asyncio.Queue[DeltaEnvelope] | None:
        """Return the delta queue for a session, or None if not registered."""
        return self._delta_queues.get(session_id)

    def ensure_queue(self, session_id: str, ws: object | None = None) -> asyncio.Queue[DeltaEnvelope]:
        """Get or create a delta queue for *session_id*, reusing *ws* if provided."""
        if session_id not in self._delta_queues:
            self._connections[session_id] = ws
            self._delta_queues[session_id] = asyncio.Queue()
        elif ws is not None and self._connections.get(session_id) is None:
            self._connections[session_id] = ws
        return self._delta_queues[session_id]

    def enqueue_user_message(self, session_id: str, content: str) -> None:
        """Enqueue a user message to be consumed by receive()."""
        msg = InputMessage(content=content, session_id=session_id, channel=WEBSOCKET_CHANNEL)
        self._message_queue.put_nowait(msg)

    def put_input_message(self, msg: InputMessage) -> None:
        """Push a fully-built InputMessage onto the receive queue.

        Used by S8 EnqueueStage via ctx.enqueue_message so the stage never
        touches a WS-specific method — it just delivers the message and the
        adapter owns its own queue.
        """
        self._message_queue.put_nowait(msg)


class WebSocketOutputAdapter(OutputAdapter):
    """Output adapter that sends streaming deltas to WebSocket connections.

    Each delta is pushed as a structured :class:`DeltaEnvelope` into the
    per-session queue managed by :class:`WebSocketInputAdapter`, so the WebUI
    server can forward it to the correct browser client.  The framework
    ``send_delta`` (plain content string) is wrapped into a ``content``
    envelope so the queue type stays uniform.
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

    async def send_envelope(self, envelope: DeltaEnvelope) -> None:
        """Enqueue a structured :class:`DeltaEnvelope` for *envelope.session_id*.

        Drops the envelope silently when no delta queue is registered (e.g. for
        cleaned-up sessions).  Subagent queues are pre-registered at dispatch
        time via the ``on_subagent_created`` callback.
        """
        q = self._input.get_delta_queue(envelope.session_id)
        if q is not None:
            q.put_nowait(envelope)

    async def send_delta(
        self, delta: str, session_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Framework ABC entry point — wraps the plain *delta* string into a
        ``content`` envelope so it travels through the same structured queue."""
        await self.send_envelope(
            DeltaEnvelope.content(
                session_id=session_id,
                agent_name=_agent_of(session_id),
                text=delta,
                metadata=metadata,
            )
        )

    async def flush_deltas(self, session_id: str) -> None:
        pass
