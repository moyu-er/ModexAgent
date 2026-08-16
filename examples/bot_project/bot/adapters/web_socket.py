"""WebSocket input/output adapters for browser-based UI."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
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

# Connection key of the anonymous pre-attach buffer created by ensure_queue().
# id() of a real WebSocketResponse is never 0, so this never collides with a
# real connection key.
_ANONYMOUS_CONN_KEY: int = 0

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
        # Multicast model: each session maps to one delta queue PER attached
        # connection (keyed by id(ws)), plus optionally one anonymous
        # pre-attach buffer under _ANONYMOUS_CONN_KEY. send_envelope fans out
        # to every queue of the session, so duplicate tabs on the same
        # conversation each receive the full stream. The registered ws objects
        # stay alive via their forward_deltas tasks / the WS handler frame,
        # so an id(ws) key can never be recycled while its queue exists.
        self._delta_queues: dict[str, dict[int, asyncio.Queue[DeltaEnvelope]]] = {}
        # Dispatch-time session genealogy (child -> parent), written once per
        # subagent dispatch via register_subagent and NEVER removed: late
        # envelopes for a session whose observers all detached must still
        # carry correct parent metadata (SessionTree / transcript genealogy).
        # Growth is bounded by the dispatch count (one short entry each) —
        # the memory that actually matters, the delta queues, is reclaimed
        # via unregister_connection / drop_anonymous_queue.
        self._parent_map: dict[str, str] = {}

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

    def register_connection(self, session_id: str, ws: object) -> asyncio.Queue[DeltaEnvelope]:
        """Register a connection for a session and return THIS connection's queue.

        Multicast: every attached connection owns one queue per session. An
        anonymous pre-attach buffer (ensure_queue) is adopted by the first
        connection to register the session, so early subagent deltas survive
        until attach. Re-registering the same connection is idempotent.
        """
        key = id(ws)
        queues = self._delta_queues.setdefault(session_id, {})
        existing = queues.get(key)
        if existing is not None:
            return existing
        pending = queues.pop(_ANONYMOUS_CONN_KEY, None)
        q = pending if pending is not None else asyncio.Queue(maxsize=DELTA_QUEUE_MAXSIZE)
        queues[key] = q
        return q

    def unregister_connection(self, session_id: str, ws: object) -> None:
        """Remove THIS connection's queue for a session.

        The session entry is dropped only once the last connection detaches,
        so a surviving duplicate tab keeps receiving the stream. The
        genealogy map is intentionally untouched (append-only).
        """
        queues = self._delta_queues.get(session_id)
        if queues is None:
            return
        queues.pop(id(ws), None)
        if not queues:
            del self._delta_queues[session_id]

    def register_subagent(self, child_session_id: str, parent_session_id: str) -> None:
        """Dispatch-time pre-registration of a dynamically-created subagent.

        One atomic seam for the two halves that must never diverge: the
        pre-attach delta buffer (so early subagent output is not dropped
        before a connection claims it) and the genealogy link (so watchers
        and reclamation can walk the conversation tree). A buffered queue
        without its parent link could never be claimed NOR reclaimed.
        """
        self.ensure_queue(child_session_id)
        self._parent_map[child_session_id] = parent_session_id

    def get_parent(self, session_id: str) -> str | None:
        """Dispatch-time parent of *session_id*, or None for root sessions."""
        return self._parent_map.get(session_id)

    def ancestors(self, session_id: str) -> Iterator[str]:
        """Parent chain of *session_id*, nearest first.

        Guards against corrupted (cyclic) links so every consumer — queue
        ownership checks, anonymous-buffer reclamation — can iterate safely.
        """
        seen: set[str] = set()
        current = self._parent_map.get(session_id)
        while current is not None and current not in seen:
            yield current
            seen.add(current)
            current = self._parent_map.get(current)

    def get_delta_queue(self, session_id: str, ws: object) -> asyncio.Queue[DeltaEnvelope] | None:
        """Return THIS connection's delta queue for a session, or None."""
        queues = self._delta_queues.get(session_id)
        if queues is None:
            return None
        return queues.get(id(ws))

    def get_delta_queues(self, session_id: str) -> list[asyncio.Queue[DeltaEnvelope]]:
        """All live queues for a session — the send_envelope fan-out set."""
        queues = self._delta_queues.get(session_id)
        return list(queues.values()) if queues else []

    def ensure_queue(self, session_id: str) -> asyncio.Queue[DeltaEnvelope]:
        """Get or create the anonymous pre-attach buffer for *session_id*.

        Subagent sessions are pre-registered at dispatch time so deltas
        emitted before any connection claims the session are buffered; the
        first connection to register adopts the buffer. When connections
        already own queues for the session, returns one of them (they
        already receive deltas via fan-out).
        """
        queues = self._delta_queues.get(session_id)
        if queues:
            return next(iter(queues.values()))
        q: asyncio.Queue[DeltaEnvelope] = asyncio.Queue(maxsize=DELTA_QUEUE_MAXSIZE)
        self._delta_queues[session_id] = {_ANONYMOUS_CONN_KEY: q}
        return q

    def drop_anonymous_queue(self, session_id: str) -> None:
        """Drop the session's pre-attach buffer when no live observer can claim it.

        Anonymous buffers bridge dispatch -> attach within a live turn. The
        buffer is reclaimed only when NO ancestor in the parent chain has a
        real connection: an attached browser's watcher claims the session
        within one poll interval, so dropping under it would lose fast
        (<1s) subagent turns — conversation_created and all deltas. Only
        buffers whose conversation tree nobody is watching (e.g. IM-driven
        turns) are dropped, keeping registry entries bounded. The genealogy
        link itself is retained (append-only); a subagent that dies without
        ever emitting turn_end still leaves its buffer — rare (emit_complete
        runs in a finally) and bounded per entry.
        """
        queues = self._delta_queues.get(session_id)
        if queues is None or set(queues) != {_ANONYMOUS_CONN_KEY}:
            return
        for ancestor in self.ancestors(session_id):
            ancestor_queues = self._delta_queues.get(ancestor)
            if ancestor_queues and set(ancestor_queues) != {_ANONYMOUS_CONN_KEY}:
                return
        del self._delta_queues[session_id]

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
        """Fan out a structured :class:`DeltaEnvelope` to every connection
        attached to *envelope.session_id*.

        Drops the envelope silently when no delta queue is registered (e.g. for
        cleaned-up sessions).  Subagent queues are pre-registered at dispatch
        time via the ``on_subagent_created`` callback.

        When a queue is full (client lagging/disconnected) the delta is
        dropped for that connection only and logged — deltas are transient UI
        refresh, never persisted, so dropping protects memory without losing
        any durable state.
        """
        for q in self._input.get_delta_queues(envelope.session_id):
            try:
                q.put_nowait(envelope)
            except asyncio.QueueFull:
                logger.warning(
                    "delta queue full for session %s; dropping delta (event_type=%s)",
                    envelope.session_id,
                    envelope.event_type,
                )
        if envelope.event_type == WebUIEventType.TURN_END.value:
            self._input.drop_anonymous_queue(envelope.session_id)

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
