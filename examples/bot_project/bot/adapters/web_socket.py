"""WebSocket input/output adapters for browser-based UI."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from bot.webui.events import DeltaEnvelope, WebUIEventType
from modex_agent.adapters.platform import StreamingMode
from modex_agent.core.session_id import SessionIdFactory, agent_of
from modex_agent.core.types import InputMessage, OutputMessage
from modex_agent.media.models import Attachment, Kind
from modex_agent.pipeline.adapters import InputAdapter, OutputAdapter

# ── Constants ──────────────────────────────────────────────────────────────

WEBSOCKET_CHANNEL: str = "websocket"

# Outbound attachment cards describe the attachment for the renderer; the
# download URL points at the G6 endpoint. The frontend holds/appends the
# ``ws`` query param (the active workspace) — the card itself is
# workspace-agnostic so it round-trips through the transcript unchanged.
_ATTACHMENT_DOWNLOAD_PATH: str = "/api/sessions/{session_id}/attachments/{attachment_id}"

# Per-session delta queue capacity. Deltas are transient UI refresh (not
# persisted); when a client lags or disconnects we drop new deltas rather than
# grow memory unbounded. Turn-level complete events do not travel this queue.
DELTA_QUEUE_MAXSIZE: int = 1024

logger = logging.getLogger(__name__)


def _agent_of(session_id: str) -> str:
    """Return the agent segment (2nd) of a full session id, default ``main``."""
    return agent_of(session_id, default="main")


def _card_kind_of(record: Attachment) -> str:
    """Map an Attachment's Kind to the renderer's two-way card kind.

    Only images render inline; every other kind (extractable documents, OTHER)
    renders as a file card. Falls back to ``"file"`` on an unknown enum value
    so a future Kind addition degrades safely.
    """
    if record.kind is Kind.IMAGE:
        return "image"
    return "file"


def _attachment_card_envelope(record: Attachment, session_id: str) -> DeltaEnvelope:
    """Build a direction-agnostic attachment-card delta for one outbound record.

    The payload carries what the renderer needs to pick inline-image vs file
    card vs fallback: ``kind`` (image/file), ``name``, ``size``, ``mime``, and
    the ``download_url``. The frontend appends the active ``ws`` query param;
    fallback-icon logic is frontend (download 404 → fallback).
    """
    download_url = _ATTACHMENT_DOWNLOAD_PATH.format(
        session_id=session_id, attachment_id=record.id
    )
    return DeltaEnvelope(
        session_id=session_id,
        agent_name=_agent_of(session_id),
        event_type=WebUIEventType.ATTACHMENT_CARD.value,
        payload={
            "attachment_id": record.id,
            "kind": _card_kind_of(record),
            "name": record.name,
            "size": record.size,
            "mime": record.mime,
            "download_url": download_url,
        },
    )


class WebSocketInputAdapter(InputAdapter):
    """Input adapter that receives messages from WebSocket connections.

    Manages per-session delta queues so the output adapter can push
    streaming deltas directly to the correct WebSocket connection.

    Each queue holds structured :class:`DeltaEnvelope` objects (not flat
    strings), serialized once at the WebSocket forwarding boundary.

    Lifetime is managed by the WebUI server, so start()/stop() are no-ops.
    """

    def __init__(self, *, session_factory: SessionIdFactory | None = None) -> None:
        super().__init__()
        self._session_factory = session_factory or SessionIdFactory()
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
        self._delta_queues[session_id] = asyncio.Queue(maxsize=DELTA_QUEUE_MAXSIZE)

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
            self._delta_queues[session_id] = asyncio.Queue(maxsize=DELTA_QUEUE_MAXSIZE)
        elif ws is not None and self._connections.get(session_id) is None:
            self._connections[session_id] = ws
        return self._delta_queues[session_id]

    def enqueue_user_message(self, session_id: str, content: str) -> None:
        """Enqueue a user message to be consumed by receive()."""
        session = self._session_factory.create(
            agent_name="main",
            external_id=session_id,
            metadata={"channel": WEBSOCKET_CHANNEL},
        )
        msg = InputMessage(content=content, session=session, channel=WEBSOCKET_CHANNEL)
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
        if message.message_type == "approval_request":
            # Structured approval push: emit the view as the envelope payload so
            # the webui renders an approval card. IM/QQ adapters (which read
            # ``content``) are unaffected — they receive the same OutputMessage
            # via their own adapters, not this branch.
            view = dict(message.metadata.get("approval") or {})
            await self.send_envelope(
                DeltaEnvelope(
                    session_id=session_id,
                    agent_name=_agent_of(session_id),
                    event_type="approval_request",
                    payload=view,
                )
            )
            return
        # Outbound attachment cards (ADR-0013 §3). Emit one card per record so
        # the frontend renders inline-image / file-card / fallback based on
        # ``kind`` and whether the download URL later succeeds. The card is
        # direction-agnostic — this adapter only describes the attachment; it
        # does not pick a rendering. The accompanying text (if any) is sent as a
        # separate content delta first so the card appears after the message.
        if message.attachment_records:
            if message.content:
                await self.send_delta(message.content, session_id)
            for record in message.attachment_records:
                await self.send_envelope(_attachment_card_envelope(record, session_id))
            return
        content = message.content or ""
        await self.send_delta(content, session_id)

    async def send_envelope(self, envelope: DeltaEnvelope) -> None:
        """Enqueue a structured :class:`DeltaEnvelope` for *envelope.session_id*.

        Drops the envelope silently when no delta queue is registered (e.g. for
        cleaned-up sessions).  Subagent queues are pre-registered at dispatch
        time via the ``on_subagent_created`` callback.

        When the queue is full (client lagging/disconnected) the delta is
        dropped and logged — deltas are transient UI refresh, never persisted,
        so dropping protects memory without losing any durable state.
        """
        q = self._input.get_delta_queue(envelope.session_id)
        if q is None:
            return
        try:
            q.put_nowait(envelope)
        except asyncio.QueueFull:
            logger.warning(
                "delta queue full for session %s; dropping delta (event_type=%s)",
                envelope.session_id,
                envelope.event_type,
            )

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
