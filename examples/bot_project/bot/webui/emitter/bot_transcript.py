"""BotTranscriptEmitter — shared turn-record lifecycle for bot emitters.

Owns the recording half of a bot turn so concrete projections (the WebUI
WebSocket emitter, the ACP editor emitter) only translate fully-factual
events into their own sink. One transcript writer per process per session:
text/reasoning accumulate as segments (keyed by ``part_id``) and are
flushed as single ``AssistantTextEvent`` / ``AssistantReasoningEvent`` at
segment boundaries; a tool call/result pair is persisted TOGETHER so both
share one ``turn_id`` and the materializer pairs them into one tool block
(also the ONLY persistence point on a resumed approval turn, where the
tool node re-emits just ``TOOL_CALL_END``).

ADR-0053 convergence: turn identity, tool-call argument pairing, and the
runtime-event mapping live on the framework
``DefaultTurnEventProjector``; this base feeds it the core ``TurnEvent``
union (ADR-0054) and derives its recording + projection behavior from the
resulting ``PresentationEvent``s. Segment accumulation for the bot's
transcript format remains bot-side (the store's materialization detail).
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

from modex_agent.adapters.emitter import StreamingAwareEmitter
from modex_agent.adapters.output import OutputAdapter
from modex_agent.agents.react.agent import ReActEvent
from modex_agent.agents.react.constants import ToolArgsDeltaPayload, ToolCallEndPayload
from modex_agent.core.emitter import AgentResult
from modex_agent.core.events import EmitterConfig
from modex_agent.core.session_id import agent_of
from modex_agent.core.turn_events import (
    ToolArgsDeltaEvent,
    TurnErroredEvent,
    TurnEvent,
    TurnFinishedEvent,
    TurnReasoningEvent,
    TurnStartedEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)
from modex_agent.messaging.models import OutputMessage
from modex_agent.presentation import (
    DefaultTurnEventProjector,
    PresentationEvent,
    TextDelta,
    ThinkingDelta,
    ToolArgsDelta,
    ToolCallStarted,
    ToolResult,
    TurnErrored,
    TurnFinished,
)

from ..events import (
    AssistantReasoningEvent,
    AssistantTextEvent,
    ModelContentDelta,
    ModelReasoningDelta,
    ServerEvent,
    SessionMeta,
)
from ..events import (
    ToolCallEvent as TcEvent,
)
from ..events import (
    ToolResultEvent as TrEvent,
)
from ..transcript_store import TranscriptStore, WorkspaceRoutedTranscriptStore

logger = logging.getLogger(__name__)


def _empty_session_meta() -> SessionMeta:
    """Default resolver: no parent session known."""
    return SessionMeta()


_REACT_TO_TURN_KINDS: dict[str, str | None] = {
    ReActEvent.MODEL_OUTPUT.value: None,
    ReActEvent.MODEL_REASONING.value: "reasoning",
    ReActEvent.TOOL_ARGS_DELTA.value: "tool_args_delta",
    ReActEvent.TOOL_CALL_START.value: "tool_call",
    ReActEvent.TOOL_CALL_END.value: "tool_result",
    ReActEvent.ITERATION_START.value: None,
    ReActEvent.ITERATION_END.value: None,
    ReActEvent.FINAL_OUTPUT.value: None,
    ReActEvent.START.value: "turn_started",
    ReActEvent.ERROR.value: "turn_errored",
    ReActEvent.MAX_ITERATIONS.value: None,
    ReActEvent.PROGRESS.value: None,
}
"""Declarative ReAct-enum → core ``TurnEvent`` kind mapping (ADR-0054).

Every ``ReActEvent`` value maps to exactly one core kind literal or
``None`` (declared ignored). ``None`` entries fall through to the
streaming base (``StreamingAwareEmitter._on_event``) unchanged:

- ``model_output``: duplicates the streaming-delta / folded-content
  entries carrying the same text (``TurnTextEvent``).
- ``iteration_start`` / ``iteration_end`` / ``progress``: intra-turn
  bookkeeping with no sink here.
- ``final_output`` / ``max_iterations``: terminal facts arrive via
  ``emit_complete`` (``TurnFinishedEvent.stop_reason``).

The architecture anchor introspects this table — no silent drops.
"""


class BotTranscriptEmitter(StreamingAwareEmitter[ReActEvent], ABC):
    """Shared turn-record lifecycle with two concrete projections.

    This base owns recording (transcript writes, segment buffering); the
    framework projector owns turn identity and tool-card pairing; the
    ``_project_*`` hooks remain the subclass sink contract, receiving the
    full-fidelity fact (never a display-truncated copy).
    """

    def __init__(
        self,
        output_adapter: OutputAdapter,
        session_id: str,
        config: EmitterConfig | None = None,
        *,
        send_timeout: float | None = None,
        pool: str | None = None,
        transcript_store: TranscriptStore | None = None,
        session_meta_resolver: Callable[[], SessionMeta] | None = None,
        sessions_dir_provider: Callable[[], Path | None] | None = None,
    ) -> None:
        super().__init__(output_adapter, session_id, config, send_timeout=send_timeout)
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
        self._sessions_dir_provider: Callable[[], Path | None] | None = (
            sessions_dir_provider
        )
        # Framework projection state: lazy turn identity, tool-argument
        # pairing, turn latency (ADR-0053).
        self._projector = DefaultTurnEventProjector(session_id, pool=pool)

        # Incremental turn state — multiple segments tracked by part_id.
        # Each part_id accumulates independently so token-level interleaving
        # (text part_1 and reasoning part_2 alternating) produces exactly 2
        # transcript events, not hundreds. Projections still fire per-token
        # (true streaming).
        self._segments: dict[str, str] = {}
        self._segment_kinds: dict[str, str] = {}
        self._segment_order: list[str] = []

    # ------------------------------------------------------------------
    # Projection contract (subclass sink formats)
    # ------------------------------------------------------------------

    @abstractmethod
    async def _project_text_delta(
        self, text: str, part_id: str | None
    ) -> None:
        """Project one content delta (full text fragment, ``part_id`` if the
        source stream identifies output parts)."""
        ...

    @abstractmethod
    async def _project_reasoning_delta(
        self, text: str, part_id: str | None
    ) -> None:
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

    async def _project_tool_args_delta(self, tool_name: str, call_id: str, args_fragment: str) -> None:
        """Project one streamed argument fragment (pre-``tool_call_start``).

        默认 no-op: 预热态是纯显示投影, 记录生命周期(段缓冲/持久化)不参与;
        WebUI 投影覆盖此钩子做节流外发, ACP 投影忽略。
        """

    @abstractmethod
    async def _project_turn_end(self, latency_ms: int, turn_id: str) -> None:
        """Project turn completion (``latency_ms`` since turn start;
        ``turn_id`` is the finished turn's id, ``""`` for an idle turn)."""
        ...

    # ------------------------------------------------------------------
    # Turn state (framework projector + bot segment accumulation)
    # ------------------------------------------------------------------

    @property
    def _current_turn_id(self) -> str:
        return self._projector.current_turn_id

    async def _persist(self, event: ServerEvent) -> None:
        if self._transcript_store is None:
            return
        sessions_dir = (
            self._sessions_dir_provider() if self._sessions_dir_provider else None
        )
        pool = self._pool or ""
        # Workspace routing is a store-shape extension boundary: only a
        # workspace-routed store multiplexes backends and accepts the dir; a
        # fixed store is already bound to its physical directory.
        if isinstance(self._transcript_store, WorkspaceRoutedTranscriptStore):
            await self._transcript_store.append(
                self._session_id, event, pool=pool, sessions_dir=sessions_dir
            )
        else:
            await self._transcript_store.append(self._session_id, event, pool=pool)

    async def _persist_partial(self, event: ServerEvent) -> None:
        """Append a streaming delta to the in-memory partial buffer.

        Routed to ``WorkspaceScopedTranscriptStore.append_partial`` (in-memory
        dict, not a file). Failure here must not break the turn — partial is
        best-effort for refresh-mid-stream; the sink push still carries the
        delta.
        """
        if self._transcript_store is None:
            return
        if not isinstance(self._transcript_store, WorkspaceRoutedTranscriptStore):
            return
        sessions_dir = (
            self._sessions_dir_provider() if self._sessions_dir_provider else None
        )
        try:
            await self._transcript_store.append_partial(
                self._session_id, event, sessions_dir=sessions_dir
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
        sessions_dir = (
            self._sessions_dir_provider() if self._sessions_dir_provider else None
        )
        try:
            await self._transcript_store.clear_partial(
                self._session_id, sessions_dir=sessions_dir
            )
        except Exception as exc:
            logger.warning(
                "partial clear failed for session %s: %s", self._session_id, exc
            )

    def _accumulate_segment(self, text: str, kind: str, part_id: str | None) -> None:
        if not text:
            return
        self._projector.ensure_turn_started()
        key = part_id if part_id else f"_{kind}"
        if key not in self._segments:
            self._segments[key] = ""
            self._segment_kinds[key] = kind
            self._segment_order.append(key)
        self._segments[key] += text

    async def _flush_active_segment(self) -> None:
        for key in self._segment_order:
            text = self._segments.get(key, "").strip()
            if not text:
                continue
            kind = self._segment_kinds.get(key, "text")
            if kind == "reasoning":
                evt: ServerEvent = AssistantReasoningEvent(
                    session_id=self._session_id,
                    agent_name=self._agent_name,
                    turn_id=self._current_turn_id,
                    text=text,
                )
            else:
                evt = AssistantTextEvent(
                    session_id=self._session_id,
                    agent_name=self._agent_name,
                    turn_id=self._current_turn_id,
                    text=text,
                )
            await self._persist(evt)
        self._segments = {}
        self._segment_kinds = {}
        self._segment_order = []
        # Clear the partial buffer here so it only holds deltas accumulated
        # since the last flush boundary (tool call / stream end). Without
        # this, the partial buffer retains ALL deltas for the entire turn —
        # including text already persisted as AssistantTextEvent — and
        # _materialize_partial_deltas produces a synthetic streaming turn
        # whose single concatenated text block duplicates the materialized
        # transcript turn's text.
        await self._clear_partial()

    # ------------------------------------------------------------------
    # Presentation dispatch (framework events -> record + projection)
    # ------------------------------------------------------------------

    async def _feed(self, event: TurnEvent) -> None:
        for presentation in self._projector.feed(event):
            await self._handle_presentation(presentation)

    async def _handle_presentation(self, event: PresentationEvent) -> None:
        match event:
            case TextDelta(text=text, segment_id=segment_id):
                self._accumulate_segment(text, "text", segment_id)
                await self._record_text_delta(text, segment_id)
            case ThinkingDelta(text=text, segment_id=segment_id):
                self._accumulate_segment(text, "reasoning", segment_id)
                await self._record_reasoning_delta(text, segment_id)
            case ToolArgsDelta(
                tool_name=tool_name, call_id=call_id, args_fragment=fragment
            ):
                # Transient warm-up signal: do not flush the text segment (the
                # body may still be semantically unfinished), do not persist to
                # the transcript, do not enter the partial buffer — refresh
                # recovery relies on the subsequent tool_call_start full
                # arguments, so losing the warm-up state is harmless.
                await self._project_tool_args_delta(tool_name, call_id, fragment)
            case ToolCallStarted(tool_name=tool_name, call_id=call_id, arguments=args):
                await self._flush_active_segment()
                await self._project_tool_start(tool_name, call_id, dict(args))
            case ToolResult(
                tool_name=tool_name,
                call_id=call_id,
                output=output,
                error=error,
                seq=seq,
                arguments=arguments,
            ):
                await self._flush_active_segment()
                # Persist call + result TOGETHER so they share a turn_id and
                # the materializer pairs them into one complete tool block.
                # This is also the ONLY persistence point on a resumed
                # approval turn (no preceding TOOL_CALL_START), where the
                # result card carries the call args. ``arguments is None``
                # marks an orphan result — persist the result alone, never a
                # fabricated empty-args call.
                if self._transcript_store is not None:
                    if arguments is not None:
                        await self._persist(
                            TcEvent(
                                session_id=self._session_id,
                                agent_name=self._agent_name,
                                turn_id=self._current_turn_id,
                                call_id=call_id,
                                tool_name=tool_name,
                                args=dict(arguments),
                            )
                        )
                    await self._persist(
                        TrEvent(
                            session_id=self._session_id,
                            agent_name=self._agent_name,
                            turn_id=self._current_turn_id,
                            call_id=call_id,
                            tool_name=tool_name,
                            result=output.strip(),
                            error=error,
                            seq=seq,
                        )
                    )
                await self._project_tool_end(tool_name, call_id, output, seq)
            case TurnErrored(message=message):
                await self._safe_adapter_send(
                    OutputMessage(content=f"Error: {message}"), log_label="emit_error"
                )
            case TurnFinished():
                # Handled inside emit_complete (flush + super + projection
                # ordering); never dispatched here.
                pass
            case _:
                # TurnStarted / usage / approval cards: no bot sink today.
                pass

    # ------------------------------------------------------------------
    # Content entry points (single-write recording + projection)
    # ------------------------------------------------------------------

    async def emit_content(self, full_content: str) -> None:
        text: str = full_content.strip()
        if text:
            self._accumulate_segment(text, "text", None)

    async def emit_delta(self, delta: str) -> None:
        if not delta:
            return
        await self._feed(TurnTextEvent(text=delta))

    async def emit_stream_end(self, resuming: bool = False) -> None:
        await self._flush_active_segment()

    async def emit_turn_event(self, event: TurnEvent) -> None:
        await self._feed(event)

    async def emit_complete(self, result: AgentResult) -> None:
        try:
            # Flush buffered segments FIRST — while the turn identity is
            # still active — then feed the terminal event (which resets the
            # projector), then forward completion and project turn end.
            await self._flush_active_segment()
            finished = self._projector.feed(
                TurnFinishedEvent(
                    stop_reason=result.stop_reason,
                    error=result.error,
                    attachments=tuple(result.attachments),
                )
            )
            await super().emit_complete(result)
            for presentation in finished:
                if isinstance(presentation, TurnFinished):
                    await self._project_turn_end(
                        presentation.latency_ms, presentation.turn_id
                    )
        finally:
            await self._clear_partial()
            self._segments = {}
            self._segment_kinds = {}
            self._segment_order = []

    async def emit_error(self, error: str) -> None:
        await self._feed(TurnErroredEvent(message=error))

    async def _on_event(self, event: ReActEvent, data: Any = None) -> None:
        """Translate the ReAct enum stream onto the core ``TurnEvent`` union.

        The disposition of every enum value is declared by
        ``_REACT_TO_TURN_KINDS``: a kind literal is translated and fed to
        the framework projector; ``None`` falls through to the streaming
        base (buffer/flush semantics unchanged).
        """
        if _REACT_TO_TURN_KINDS.get(event.value) is None:
            await super()._on_event(event, data)
            return
        match event:
            case ReActEvent.MODEL_REASONING:
                await self._feed(TurnReasoningEvent(text=data))
            case ReActEvent.TOOL_ARGS_DELTA:
                payload: ToolArgsDeltaPayload = data
                await self._feed(
                    ToolArgsDeltaEvent(
                        call_id=payload.call_id,
                        tool_name=payload.tool_name,
                        args_fragment=payload.args_fragment,
                    )
                )
            case ReActEvent.TOOL_CALL_START:
                # The tool node canonicalizes call_id before emitting
                # (assigning one when the provider omits it), so the id here
                # is the SAME id the later END will carry — pass it through
                # verbatim.
                await self._feed(
                    TurnToolCallEvent(
                        tool_name=data.tool_name,
                        call_id=data.call_id,
                        arguments=data.arguments or {},
                    )
                )
            case ReActEvent.TOOL_CALL_END:
                end_payload: ToolCallEndPayload = data
                tool_call = end_payload.tool_call
                if tool_call.call_id:
                    await self._feed(
                        TurnToolResultEvent(
                            tool_name=tool_call.tool_name,
                            call_id=tool_call.call_id,
                            output=end_payload.result.message_content(),
                            error=end_payload.result.error,
                            seq=end_payload.seq,
                            arguments=tool_call.arguments,
                        )
                    )
                else:
                    # Degenerate call without identity (provider omitted the
                    # id and canonicalization failed): the neutral seam
                    # requires call identity, so record + project directly —
                    # the legacy wire bytes (``call_id`` omitted) preserved.
                    await self._flush_active_segment()
                    if self._transcript_store is not None:
                        self._projector.ensure_turn_started()
                        await self._persist(
                            TcEvent(
                                session_id=self._session_id,
                                agent_name=self._agent_name,
                                turn_id=self._current_turn_id,
                                call_id=tool_call.call_id,
                                tool_name=tool_call.tool_name,
                                args=tool_call.arguments or {},
                            )
                        )
                        await self._persist(
                            TrEvent(
                                session_id=self._session_id,
                                agent_name=self._agent_name,
                                turn_id=self._current_turn_id,
                                call_id=tool_call.call_id,
                                tool_name=tool_call.tool_name,
                                result=end_payload.result.message_content().strip(),
                                error=end_payload.result.error,
                                seq=end_payload.seq,
                            )
                        )
                    await self._project_tool_end(
                        tool_call.tool_name,
                        tool_call.call_id,
                        end_payload.result.message_content(),
                        end_payload.seq,
                    )
            case ReActEvent.ERROR:
                await self._feed(TurnErroredEvent(message=str(data)))
            case ReActEvent.START:
                # Eager turn identity: the projector assigns the turn id on
                # turn_started instead of waiting for the first content
                # event. No bot-side record or wire frame derives from it.
                await self._feed(TurnStartedEvent())
            case _:
                # Unreachable: the table maps every value, None entries
                # returned to the streaming base above.
                await super()._on_event(event, data)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _record_text_delta(self, text: str, part_id: str | None) -> None:
        """Partial-buffer record + projection for one content delta."""
        evt = self._content_delta_event(text, part_id)
        await self._persist_partial(evt)
        await self._project_text_delta(text, part_id)

    async def _record_reasoning_delta(self, text: str, part_id: str | None) -> None:
        """Partial-buffer record + projection for one reasoning delta."""
        evt = self._reasoning_delta_event(text, part_id)
        await self._persist_partial(evt)
        await self._project_reasoning_delta(text, part_id)

    def _content_delta_event(self, text: str, part_id: str | None) -> ServerEvent:
        """WebUI-shaped content delta for the partial buffer (one schema).

        Kept WebUI-shaped because the partial buffer is consumed by the
        WebUI's refresh-mid-stream materialization; ACP shares the same
        in-memory record without projecting it.
        """
        return ModelContentDelta(
            session_id=self._session_id,
            agent_name=self._agent_name,
            text=text,
            turn_id=self._current_turn_id,
            segment_id=part_id if part_id else "_text",
        )

    def _reasoning_delta_event(self, text: str, part_id: str | None) -> ServerEvent:
        """WebUI-shaped reasoning delta for the partial buffer."""
        return ModelReasoningDelta(
            session_id=self._session_id,
            agent_name=self._agent_name,
            text=text,
            turn_id=self._current_turn_id,
            segment_id=part_id if part_id else "_reasoning",
        )

    def set_sessions_dir_provider(
        self, provider: Callable[[], Path | None] | None
    ) -> None:
        """Inject the per-workspace sessions_dir resolver (resolver cell).

        Called at emitter creation by pool_builder's per-pool emitter-factory
        wrapper. Once set, every transcript append uses the resolved dir instead
        of the fallible bind_workspace_root ctxvar.
        """
        self._sessions_dir_provider = provider

    def _metadata(self) -> dict[str, object]:
        """Cross-cutting context attached to every emitted envelope."""
        return {"turn_id": self._current_turn_id}
