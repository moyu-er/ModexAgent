"""BotTranscriptEmitter — shared turn-record lifecycle for bot sinks.

Owns the recording half of a bot turn so concrete projections (the WebUI
WebSocket emitter, the ACP editor emitter) only translate fully-factual
events into their own sink. One transcript writer per process per session:
text/reasoning accumulate as segments (keyed by ``segment_id``) and are
persisted as single ``TextDelta`` / ``ThinkingDelta`` records at segment
boundaries; a tool call/result pair is persisted TOGETHER (a
``ToolCallStarted`` record ahead of the received ``ToolResult``) so both
share one ``turn_id`` and the framework folder pairs them into one tool
block (also the ONLY persistence point on a resumed approval turn, where
the tool node re-emits just the tool result).

The durable record is the framework ``PresentationEvent`` (the W6
transcript cutover): terminal/approval/usage records persist as received,
``tool_args_delta`` never touches the store (transient warm-up), and no
``ServerEvent`` is written to any store — the wire projection is the only
remaining ``ServerEvent`` producer.

The input face is the core ``TurnEvent`` union (the framework's single
event stream): this base delegates projection + fan-out to the framework
:class:`~modex_agent.presentation.SessionEventHub` it owns — the hub runs
the ``DefaultTurnEventProjector`` and fans the resulting
``PresentationEvent``s out to two registered consumers, in registration
order:

1. this emitter itself — the transcript tap (segment accumulation,
   transcript/partial persistence, error render);
2. the projection bridge — one ``PresentationSink`` adapter calling the
   concrete sink's ``_project_*`` hooks (WebUI wire frames, ACP
   full-fidelity forwarding).

The bot keeps only bot-specific work: the ServerEvent wire projection,
the transcript record codec, and the terminal channel composition (error
render / attachments via the output adapter) stay on this class.
Projections receive full-fidelity facts (full tool args, full result,
``seq``, ``part_id``) — display truncation is each projection's own
concern.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

from modex_agent.adapters.emitter import BufferingSink
from modex_agent.adapters.output import OutputAdapter
from modex_agent.core.emitter import KindGate, TurnBinding
from modex_agent.core.llm_struct import TokenUsage
from modex_agent.core.session_id import agent_of
from modex_agent.core.turn_events import (
    IterationFinishedEvent,
    StopReason,
    TurnEvent,
    TurnFinishedEvent,
)
from modex_agent.messaging.models import OutputMessage
from modex_agent.presentation import (
    ApprovalRequested,
    ApprovalResolved,
    PresentationEvent,
    PresentationSink,
    SessionEventHub,
    TextDelta,
    ThinkingDelta,
    ToolArgsDelta,
    ToolCallStarted,
    ToolResult,
    TurnErrored,
    TurnFinished,
    UsageSummary,
)

from ..events import SessionMeta
from ..transcript_store import TranscriptRecord, TranscriptStore, WorkspaceRoutedTranscriptStore

logger = logging.getLogger(__name__)


def _empty_session_meta() -> SessionMeta:
    """Default resolver: no parent session known."""
    return SessionMeta()


class BotTranscriptEmitter(BufferingSink, PresentationSink, ABC):
    """Shared turn-record lifecycle with two concrete projections.

    This base is the hub's transcript tap (``handle`` records every
    projected presentation event); the framework hub owns turn identity
    and tool-card pairing; the ``_project_*`` hooks remain the subclass
    sink contract, driven by the hub's projection-bridge consumer and
    receiving the full-fidelity fact (never a display-truncated copy).
    """

    def __init__(
        self,
        output_adapter: OutputAdapter,
        session_id: str,
        gate: KindGate | None = None,
        *,
        send_timeout: float | None = None,
        pool: str | None = None,
        transcript_store: TranscriptStore | None = None,
        session_meta_resolver: Callable[[], SessionMeta] | None = None,
        sessions_dir_provider: Callable[[], Path | None] | None = None,
        turn_id: str = "",
        resumed: bool = False,
    ) -> None:
        super().__init__(
            output_adapter,
            session_id,
            gate,
            send_timeout=send_timeout,
        )
        # session_id is the FULL receiver-owned identifier shared with the
        # memory system: {conv}.{agent}[.{invocation_id}].  Keep it verbatim so
        # every emitted event and the persisted transcript carry the complete
        # id — two subagent invocations never collapse into one transcript.
        self._session_id: str = session_id
        self._agent_name: str = agent_of(session_id, default="main")
        self._pool: str | None = pool
        self._transcript_store: TranscriptStore | None = transcript_store
        # Lazy resolver for parent_session_id only. Pool ownership is fixed by
        # the factory when this emitter is constructed.
        self._session_meta_resolver = session_meta_resolver or _empty_session_meta
        # Resolver-cell-driven workspace resolution for transcript writes. When
        # set, the owning workspace's sessions_dir is resolved per write from
        # the per-workspace resolver cell (same source memory uses) — this
        # survives the broker-queue task boundary where the bind_workspace_root
        # ContextVar is lost. None = fall back to the ctxvar (legacy/tests).
        self._sessions_dir_provider: Callable[[], Path | None] | None = sessions_dir_provider
        # Framework per-turn station: core TurnEvents in, presentation events
        # fanned out to (this recording tap, the projection bridge) —
        # registration order fixes record-before-project delivery. The binding
        # comes from the sink factory: ``turn_id`` adopts the runtime's turn
        # identity (so an approval resume — ``resumed=True`` carrying the
        # suspended attempt's id — continues the SAME turn on the wire and
        # emits no second TurnStarted); empty keeps the projector's lazy
        # factory, matching the pre-hub wire behavior.
        self._event_hub = SessionEventHub(
            TurnBinding(
                session_id=session_id,
                agent_name=self._agent_name,
                pool=pool,
                turn_id=turn_id,
                resumed=resumed,
            ),
            (self, _ProjectionBridge(self)),
        )

        # Incremental turn state — multiple segments tracked by segment id.
        # Each segment accumulates independently so token-level interleaving
        # (text part_1 and reasoning part_2 alternating) persists exactly 2
        # transcript records, not hundreds. Projections still fire per-token
        # (true streaming). ``_segment_heads`` remembers each segment's
        # first received delta so the flushed record reuses its identity
        # envelope + projector timestamp.
        self._segments: dict[str, str] = {}
        self._segment_kinds: dict[str, str] = {}
        self._segment_order: list[str] = []
        self._segment_heads: dict[str, TextDelta | ThinkingDelta] = {}
        # Per-turn flag: a mid-flight turn_errored already carried the
        # user-facing error message, so the terminal render is suppressed.
        self._error_delivered = False

    # ------------------------------------------------------------------
    # Projection contract (subclass sink formats)
    # ------------------------------------------------------------------

    @abstractmethod
    async def _project_text_delta(self, text: str, part_id: str | None) -> None:
        """Project one content delta (full text fragment, ``part_id`` if the
        source stream identifies output parts)."""
        ...

    @abstractmethod
    async def _project_reasoning_delta(self, text: str, part_id: str | None) -> None:
        """Project one reasoning delta."""
        ...

    @abstractmethod
    async def _project_tool_start(
        self,
        tool_name: str,
        call_id: str,
        full_args: dict[str, Any],  # open extension payload — tool schemas evolve
    ) -> None:
        """Project a tool call start with the FULL argument dict."""
        ...

    @abstractmethod
    async def _project_tool_end(
        self,
        tool_name: str,
        call_id: str | None,
        full_result: str,
        seq: int | None,
    ) -> None:
        """Project a tool call end with the FULL result text and ``seq``."""
        ...

    async def _project_tool_args_delta(
        self, tool_name: str, call_id: str, args_fragment: str
    ) -> None:
        """Project one streamed argument fragment (pre-``tool_call``).

        默认 no-op: 预热态是纯显示投影, 记录生命周期(段缓冲/持久化)不参与;
        WebUI 投影覆盖此钩子做节流外发, ACP 投影忽略。
        """

    async def _project_approval_requested(
        self, tool_name: str, call_id: str, prompt: str
    ) -> None:
        """Project one approval-requested card (a tool call awaits a human).

        No-op default: approval delivery is each sink's channel concern (the
        IM text prompt rides the ApprovalUserInterface output path); the WebUI
        projection overrides this hook to stream the card.
        """

    async def _project_approval_resolved(self, call_id: str, approved: bool) -> None:
        """Project one approval decision (the decision flowed back).

        No-op default; the WebUI projection overrides this hook to stream the
        decided card.
        """

    async def _project_usage_summary(self, usage: TokenUsage) -> None:
        """Project one token-usage snapshot for the turn.

        No-op default: the recording lifecycle takes no part; the WebUI
        projection overrides this hook to stream the usage card.
        """

    @abstractmethod
    async def _project_turn_end(self, latency_ms: int, turn_id: str) -> None:
        """Project turn completion (``latency_ms`` since turn start;
        ``turn_id`` is the finished turn's id, ``""`` for an idle turn)."""
        ...

    # ------------------------------------------------------------------
    # Turn state (hub projector + bot segment accumulation)
    # ------------------------------------------------------------------

    @property
    def _current_turn_id(self) -> str:
        return self._event_hub.projector.current_turn_id

    async def _persist(self, record: TranscriptRecord) -> None:
        if self._transcript_store is None:
            return
        sessions_dir = self._sessions_dir_provider() if self._sessions_dir_provider else None
        pool = self._pool or ""
        # Workspace routing is a store-shape extension boundary: only a
        # workspace-routed store multiplexes backends and accepts the dir; a
        # fixed store is already bound to its physical directory.
        if isinstance(self._transcript_store, WorkspaceRoutedTranscriptStore):
            await self._transcript_store.append(
                self._session_id, record, pool=pool, sessions_dir=sessions_dir
            )
        else:
            await self._transcript_store.append(self._session_id, record, pool=pool)

    async def _persist_partial(self, record: TranscriptRecord) -> None:
        """Append a streaming delta record to the in-memory partial buffer.

        Routed to ``WorkspaceScopedTranscriptStore.append_partial`` (in-memory
        dict, not a file). Failure here must not break the turn — partial is
        best-effort for refresh-mid-stream; the sink push still carries the
        delta.
        """
        if self._transcript_store is None:
            return
        if not isinstance(self._transcript_store, WorkspaceRoutedTranscriptStore):
            return
        sessions_dir = self._sessions_dir_provider() if self._sessions_dir_provider else None
        try:
            await self._transcript_store.append_partial(
                self._session_id, record, sessions_dir=sessions_dir
            )
        except Exception as exc:
            logger.warning(
                "partial persist failed for session %s: %s; refresh-mid-stream may lose this delta",
                self._session_id,
                exc,
            )

    async def _clear_partial(self) -> None:
        """Drop the in-memory partial buffer for this session (turn ended)."""
        if self._transcript_store is None:
            return
        if not isinstance(self._transcript_store, WorkspaceRoutedTranscriptStore):
            return
        sessions_dir = self._sessions_dir_provider() if self._sessions_dir_provider else None
        try:
            await self._transcript_store.clear_partial(self._session_id, sessions_dir=sessions_dir)
        except Exception as exc:
            logger.warning("partial clear failed for session %s: %s", self._session_id, exc)

    def _accumulate_segment(self, event: TextDelta | ThinkingDelta, kind: str) -> None:
        """Accumulate one streamed delta into its segment (and remember the head)."""
        if not event.text:
            return
        self._event_hub.projector.ensure_turn_started()
        key = event.segment_id
        if key not in self._segments:
            self._segments[key] = ""
            self._segment_kinds[key] = kind
            self._segment_order.append(key)
            self._segment_heads[key] = event
        self._segments[key] += event.text

    async def _flush_active_segment(self) -> None:
        """Persist each accumulated segment as one complete delta record.

        The flushed record reuses the segment head's identity envelope and
        the projector's timestamp, carries the segment's full (stripped)
        text, and keeps the runtime segment id — the framework folder then
        merges replayed records exactly as it folds the live stream.
        """
        for key in self._segment_order:
            text = self._segments.get(key, "").strip()
            head = self._segment_heads.get(key)
            if not text or head is None:
                continue
            kind = self._segment_kinds.get(key, "text")
            record: TranscriptRecord
            if kind == "reasoning":
                record = ThinkingDelta(
                    session_id=head.session_id,
                    agent_name=head.agent_name,
                    turn_id=self._current_turn_id,
                    timestamp_ms=head.timestamp_ms,
                    text=text,
                    segment_id=key,
                )
            else:
                record = TextDelta(
                    session_id=head.session_id,
                    agent_name=head.agent_name,
                    turn_id=self._current_turn_id,
                    timestamp_ms=head.timestamp_ms,
                    text=text,
                    segment_id=key,
                )
            await self._persist(record)
        self._segments = {}
        self._segment_kinds = {}
        self._segment_order = []
        self._segment_heads = {}
        # Clear the partial buffer here so it only holds deltas accumulated
        # since the last flush boundary (tool call / stream end). Without
        # this, the partial buffer retains ALL deltas for the entire turn —
        # including text already persisted as a flushed record — and the
        # partial replay produces a synthetic streaming turn whose single
        # concatenated text block duplicates the materialized transcript
        # turn's text.
        await self._clear_partial()

    # ------------------------------------------------------------------
    # Transcript tap (PresentationSink face: record every projected event)
    # ------------------------------------------------------------------

    async def handle(self, event: PresentationEvent) -> None:
        match event:
            case TextDelta():
                self._accumulate_segment(event, "text")
                await self._persist_partial(event)
            case ThinkingDelta():
                self._accumulate_segment(event, "reasoning")
                await self._persist_partial(event)
            case ToolArgsDelta():
                # Transient warm-up signal: no record here — do not flush the
                # text segment (the body may still be semantically finished),
                # do not persist to the transcript, do not enter the partial
                # buffer (refresh recovery relies on the subsequent
                # tool_call full arguments, so losing the warm-up state is
                # harmless). The throttled wire projection rides the bridge
                # consumer.
                pass
            case ToolCallStarted():
                # Segment boundary before the tool card opens; the wire
                # projection rides the bridge consumer (registration order).
                await self._flush_active_segment()
            case ToolResult(
                tool_name=tool_name,
                call_id=call_id,
                arguments=arguments,
            ):
                await self._flush_active_segment()
                # Persist call + result TOGETHER so they share a turn_id and
                # the framework folder pairs them into one complete tool
                # block. This is also the ONLY persistence point on a resumed
                # approval turn (no preceding tool_call), where the received
                # result card carries the call args. ``arguments is None``
                # marks an orphan result — persist the result alone, never a
                # fabricated empty-args call.
                if self._transcript_store is not None:
                    if arguments is not None:
                        await self._persist(
                            ToolCallStarted(
                                session_id=event.session_id,
                                agent_name=event.agent_name,
                                turn_id=self._current_turn_id,
                                timestamp_ms=event.timestamp_ms,
                                tool_name=tool_name,
                                call_id=call_id,
                                arguments=dict(arguments),
                            )
                        )
                    await self._persist(event)
            case TurnErrored(message=message):
                # Mid-flight terminal: the error render goes through the
                # SAME once-per-turn helper the terminal branch uses, so
                # the render and its flag never drift between the two
                # terminal paths this emitter owns.
                await self._render_turn_error(message)
            case TurnFinished():
                # Terminal record — stop classification + latency. An idle
                # turn (no identity) has no replayable surface: today's
                # transcripts never carried one, so it stays unpersisted.
                if event.turn_id:
                    await self._persist(event)
            case ApprovalRequested() | ApprovalResolved() | UsageSummary():
                await self._persist(event)
            case _:
                # TurnStarted has no bot record: turn grouping rides the
                # record identity envelope (turn_id) alone.
                pass

    # ------------------------------------------------------------------
    # Turn-event dispatch (the sink face)
    # ------------------------------------------------------------------

    async def _dispatch(self, event: TurnEvent) -> None:
        match event:
            case TurnFinishedEvent():
                await self._handle_turn_finished(event)
            case IterationFinishedEvent():
                # Segment boundary: the retired emit_stream_end flush now
                # rides the iteration-finished signal (same boundary — after
                # each LLM output's iteration closes). The projector
                # declares this kind ignored — nothing to project.
                await self._flush_active_segment()
            case _:
                await self._event_hub.emit(event)

    async def _render_turn_error(self, message: str) -> None:
        """Deliver the user-facing error render once per turn.

        THE single owner of the error render for both terminal paths this
        emitter owns — the mid-flight tap (``TurnErrored``) and the
        terminal branch (``TurnFinished`` with ``StopReason.ERROR``):
        whichever fires first renders, the other is suppressed by the
        once flag. Sends through the inherited adapter-safe send (the
        same protected method the base ``BufferingSink._dispatch`` renders
        through).
        """
        if self._error_delivered:
            return
        self._error_delivered = True
        await self._safe_adapter_send(
            OutputMessage(content=f"Error: {message}"), log_label="emit_error"
        )

    async def _handle_turn_finished(self, event: TurnFinishedEvent) -> None:
        """Terminal branch: error render, segment flush, attachments, projection.

        Ordering preserved from the retired emitter channels: the error
        message (when the mid-flight path did not already deliver one),
        then the segment flush (while the turn identity is still active),
        then attachments (the inherited base delivery), then the terminal
        projection — delegating the event to the hub projects it and fans
        the TurnFinished card out to the consumers (the projection bridge
        renders the terminal frame last). The event is forwarded VERBATIM,
        never reconstructed field-by-field, so future fields survive.
        """
        try:
            if event.stop_reason is StopReason.ERROR and event.error:
                await self._render_turn_error(event.error)
            await self._flush_active_segment()
            await self._deliver_attachments(event.attachments)
            await self._event_hub.emit(event)
        finally:
            await self._clear_partial()
            self._segments = {}
            self._segment_kinds = {}
            self._segment_order = []
            self._segment_heads = {}
            self._error_delivered = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def set_sessions_dir_provider(self, provider: Callable[[], Path | None] | None) -> None:
        """Inject the per-workspace sessions_dir resolver (resolver cell).

        Called at emitter creation by pool_builder's per-pool emitter-factory
        wrapper. Once set, every transcript append uses the resolved dir instead
        of the fallible bind_workspace_root ctxvar.
        """
        self._sessions_dir_provider = provider

    def _metadata(self) -> dict[str, object]:
        """Cross-cutting context attached to every emitted envelope."""
        return {"turn_id": self._current_turn_id}


class _ProjectionBridge(PresentationSink):
    """The hub's projection consumer: presentation events -> sink hooks.

    One ``PresentationSink`` adapter bridging the concrete emitter's
    ``_project_*`` hooks (the bot sink contract: WebUI wire frames, ACP
    full-fidelity forwarding) onto the hub's consumer face. Registered
    after the transcript tap, so every fact is recorded before it is
    projected — the delivery order both sink formats relied on before
    the hub extraction.
    """

    def __init__(self, emitter: BotTranscriptEmitter) -> None:
        self._emitter = emitter

    async def handle(self, event: PresentationEvent) -> None:
        emitter = self._emitter
        match event:
            case TextDelta(text=text, segment_id=segment_id):
                await emitter._project_text_delta(text, segment_id)
            case ThinkingDelta(text=text, segment_id=segment_id):
                await emitter._project_reasoning_delta(text, segment_id)
            case ToolArgsDelta(tool_name=tool_name, call_id=call_id, args_fragment=fragment):
                await emitter._project_tool_args_delta(tool_name, call_id, fragment)
            case ToolCallStarted(tool_name=tool_name, call_id=call_id, arguments=args):
                await emitter._project_tool_start(tool_name, call_id, dict(args))
            case ToolResult(tool_name=tool_name, call_id=call_id, output=output, seq=seq):
                await emitter._project_tool_end(tool_name, call_id, output, seq)
            case ApprovalRequested(
                tool_name=tool_name, call_id=call_id, prompt=prompt
            ):
                await emitter._project_approval_requested(tool_name, call_id, prompt)
            case ApprovalResolved(call_id=call_id, approved=approved):
                await emitter._project_approval_resolved(call_id, approved)
            case UsageSummary(usage=usage):
                await emitter._project_usage_summary(usage)
            case TurnFinished(latency_ms=latency_ms, turn_id=turn_id):
                await emitter._project_turn_end(latency_ms, turn_id)
            case _:
                # TurnStarted / TurnErrored have no sink projection hook (the
                # error render lives on the transcript tap; TurnStarted has no
                # bot wire frame).
                pass
