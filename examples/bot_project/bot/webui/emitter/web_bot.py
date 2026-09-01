"""WebBotEmitter — streaming event emitter for WebUI WebSocket sessions.

Streaming phase:
  - ``emit_delta`` / ``_on_event`` → push incremental JSON events via WebSocket.
  - Deltas are NOT persisted individually — they are transient UI updates.

Buffered persistence (flushed at stream/turn boundaries):
  - ``emit_delta`` / ``emit_content`` → accumulate clean LLM text in a buffer.
  - ``emit_stream_end`` → flush the buffer as a single ``AssistantTextEvent``.
  - ``emit_complete`` → flush any remaining buffer, then send ``turn_end``.
  - ``_on_event`` TOOL_CALL_START / TOOL_CALL_END → persist ``ToolCallEvent`` /
    ``ToolResultEvent`` immediately.
  - ``_ensure_turn_started`` → lazily creates the turn UUID; ``TurnStartEvent`` is
    WebSocket-only and is NOT persisted.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from modex_agent.agents.react.agent import ReActEvent
from modex_agent.agents.react.constants import ToolCallEndPayload
from modex_agent.core.emitter import AgentResult, EmitterConfig, StreamingAwareEmitter
from modex_agent.core.session_id import agent_of
from modex_agent.core.turn_events import (
    TurnEvent,
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)

from ...adapters.web_socket import WebSocketOutputAdapter
from ..events import (
    ModelContentDelta,
    ModelReasoningDelta,
    ServerEvent,
    SessionMeta,
    ToolCallEndEvent,
    ToolCallStartEvent,
    TurnEndEvent,
)
from ..events import (
    ToolCallEvent as TcEvent,
)
from ..events import (
    ToolResultEvent as TrEvent,
)
from ..transcript_store import TranscriptStore
from ._segments import (
    _MAX_TOOL_RESULT_LEN,
    _MODEL_REASONING,
    _TOOL_CALL_END,
    _TOOL_CALL_START,
    _accumulate_segment,
    _empty_session_meta,
    _flush_active_segment,
    _send_event,
    _truncate_tool_args,
)

logger = logging.getLogger(__name__)


class WebBotEmitter(StreamingAwareEmitter[ReActEvent]):
    """Streaming emitter for WebUI.

    - Sends incremental deltas via WebSocket for real-time UI rendering.
    - Collects full content / reasoning / tool traces during the turn.
    - At ``emit_complete``, persists the complete turn to the transcript
      store and notifies the client via ``turn_end``.
    """

    # Extracted to ._segments; assigned as class attrs so self. call-sites and
    # instance overrides (tests monkeypatch emitter._flush_active_segment) hold.
    _accumulate_segment = _accumulate_segment
    _flush_active_segment = _flush_active_segment
    _send_event = _send_event

    def __init__(
        self,
        output_adapter: WebSocketOutputAdapter,
        session_id: str,
        config: EmitterConfig | None = None,
        *,
        pool: str | None = None,
        send_timeout: float | None = None,
        transcript_store: TranscriptStore | None = None,
        session_meta_resolver: Callable[[], SessionMeta] | None = None,
        sessions_dir_provider: Callable[[], Path | None] | None = None,
    ) -> None:
        super().__init__(output_adapter, session_id, config, send_timeout=send_timeout)
        self._output: WebSocketOutputAdapter = output_adapter
        # session_id is the FULL receiver-owned identifier shared with the
        # memory system: {conv}.{agent}[.{invocation_id}].  Keep it verbatim so
        # every emitted event and the persisted transcript carry the complete
        # id — two subagent invocations never collapse into one transcript.
        self._session_id: str = session_id
        self._agent_name: str = agent_of(session_id, default="main")
        self._pool: str | None = pool
        self._turn_counter: int = 1
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

        # Incremental turn state — multiple segments tracked by part_id.
        # Each part_id accumulates independently so token-level interleaving
        # (text part_1 and reasoning part_2 alternating) produces exactly 2
        # transcript events, not hundreds. WebSocket deltas still fire
        # per-token (true SSE).
        self._segments: dict[str, str] = {}
        self._segment_kinds: dict[str, str] = {}
        self._segment_order: list[str] = []
        self._current_turn_id: str = ""
        self._turn_active: bool = False
        self._turn_started_at: float = time.time()
        self._pending_external_tools: dict[str, tuple[str, dict[str, object]]] = {}

    # ------------------------------------------------------------------
    # Turn lifecycle helpers
    # ------------------------------------------------------------------

    async def _persist(self, event: ServerEvent) -> None:
        if self._transcript_store is None:
            return
        sessions_dir = (
            self._sessions_dir_provider() if self._sessions_dir_provider else None
        )
        pool = self._pool or ""
        if sessions_dir is not None:
            await self._transcript_store.append(
                self._session_id, event, pool=pool, sessions_dir=sessions_dir
            )
        else:
            await self._transcript_store.append(self._session_id, event, pool=pool)

    async def _persist_partial(self, event: ServerEvent) -> None:
        """Append a streaming delta to the in-memory partial buffer.

        Routed to ``WorkspaceScopedTranscriptStore.append_partial`` (in-memory
        dict, not a file). Failure here must not break the turn — partial is
        best-effort for refresh-mid-stream; the WS push still carries the delta.
        """
        if self._transcript_store is None:
            return
        append_partial = getattr(self._transcript_store, "append_partial", None)
        if append_partial is None:
            return
        sessions_dir = (
            self._sessions_dir_provider() if self._sessions_dir_provider else None
        )
        try:
            if sessions_dir is not None:
                await append_partial(self._session_id, event, sessions_dir=sessions_dir)
            else:
                await append_partial(self._session_id, event)
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
        clear_partial = getattr(self._transcript_store, "clear_partial", None)
        if clear_partial is None:
            return
        sessions_dir = (
            self._sessions_dir_provider() if self._sessions_dir_provider else None
        )
        try:
            if sessions_dir is not None:
                await clear_partial(self._session_id, sessions_dir=sessions_dir)
            else:
                await clear_partial(self._session_id)
        except Exception as exc:
            logger.warning(
                "partial clear failed for session %s: %s", self._session_id, exc
            )

    def _ensure_turn_started(self) -> None:
        """Lazily start a new turn with UUID turn_id.

        ``TurnStartEvent`` is sent to the WebSocket for the frontend to
        render turn boundaries; it is NOT persisted to the transcript store
        because it carries no conversational content.
        """
        if self._turn_active:
            return
        self._current_turn_id = uuid.uuid4().hex[:12]
        self._turn_active = True
        self._turn_started_at = time.time()

    # ------------------------------------------------------------------
    # Streaming (transient WS deltas + incremental persistence)
    # ------------------------------------------------------------------

    async def emit_content(self, full_content: str) -> None:
        self._ensure_turn_started()
        text: str = full_content.strip()
        if text:
            self._accumulate_segment(text, "text", None)

    async def emit_delta(self, delta: str) -> None:
        if not delta:
            return
        self._ensure_turn_started()
        self._accumulate_segment(delta, "text", None)
        evt = ModelContentDelta(
            session_id=self._session_id,
            agent_name=self._agent_name,
            text=delta,
            turn_id=self._current_turn_id,
            segment_id="_text",
        )
        await self._persist_partial(evt)
        await self._send_event(evt)

    async def emit_stream_end(self, resuming: bool = False) -> None:
        await self._flush_active_segment()

    # ------------------------------------------------------------------
    # Turn complete (flush + notify)
    # ------------------------------------------------------------------

    async def emit_complete(self, result: AgentResult) -> None:
        try:
            await self._flush_active_segment()
            await super().emit_complete(result)
            latency_ms: int = int((time.time() - self._turn_started_at) * 1000)

            ws_turn_end = TurnEndEvent(
                session_id=self._session_id,
                agent_name=self._agent_name,
                turn_id=self._current_turn_id if self._turn_active else "",
                latency_ms=latency_ms,
            )
            await self._send_event(ws_turn_end)
        finally:
            await self._clear_partial()
            self._segments = {}
            self._segment_kinds = {}
            self._segment_order = []
            self._pending_external_tools = {}
            self._turn_active = False
            self._turn_started_at = time.time()
            self._turn_counter += 1

    async def emit_error(self, error: str) -> None:
        """Notify that an error occurred, then delegate to parent."""
        await super().emit_error(error)

    async def emit_turn_event(self, event: TurnEvent) -> None:
        match event:
            case TurnTextEvent(text=text, part_id=part_id):
                self._accumulate_segment(text, "text", part_id)
                segment_id = part_id if part_id else "_text"
                evt = ModelContentDelta(
                    session_id=self._session_id,
                    agent_name=self._agent_name,
                    text=text,
                    turn_id=self._current_turn_id,
                    segment_id=segment_id,
                )
                await self._persist_partial(evt)
                await self._send_event(evt)
            case TurnReasoningEvent(text=text, part_id=part_id):
                self._ensure_turn_started()
                self._accumulate_segment(text, "reasoning", part_id)
                segment_id = part_id if part_id else "_reasoning"
                delta_evt = ModelReasoningDelta(
                    session_id=self._session_id,
                    agent_name=self._agent_name,
                    text=text,
                    turn_id=self._current_turn_id,
                    segment_id=segment_id,
                )
                await self._persist_partial(delta_evt)
                await self._send_event(delta_evt)
            case TurnToolCallEvent(
                tool_name=tool_name, call_id=call_id, arguments=arguments
            ):
                await self._flush_active_segment()
                self._ensure_turn_started()
                full_args: dict[str, object] = dict(arguments)
                self._pending_external_tools[call_id] = (tool_name, full_args)
                await self._send_event(
                    ToolCallStartEvent(
                        session_id=self._session_id,
                        agent_name=self._agent_name,
                        tool=tool_name,
                        args=_truncate_tool_args(full_args),
                        turn_id=self._current_turn_id,
                        call_id=call_id,
                    )
                )
            case TurnToolResultEvent(
                tool_name=tool_name, call_id=call_id, output=output
            ):
                await self._flush_active_segment()
                self._ensure_turn_started()
                pending = self._pending_external_tools.pop(call_id, None)
                full_args = pending[1] if pending is not None else {}
                if self._transcript_store is not None:
                    if pending is not None:
                        await self._persist(
                            TcEvent(
                                session_id=self._session_id,
                                agent_name=self._agent_name,
                                turn_id=self._current_turn_id,
                                call_id=call_id,
                                tool_name=tool_name,
                                args=full_args,
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
                        )
                    )
                result_summary = (
                    output[:_MAX_TOOL_RESULT_LEN] + "..."
                    if len(output) > _MAX_TOOL_RESULT_LEN
                    else output
                )
                await self._send_event(
                    ToolCallEndEvent(
                        session_id=self._session_id,
                        agent_name=self._agent_name,
                        tool=tool_name,
                        result_summary=result_summary,
                        turn_id=self._current_turn_id,
                        call_id=call_id,
                    )
                )

    # ------------------------------------------------------------------
    # Event dispatch (ReActEvent → ServerEvent)
    # ------------------------------------------------------------------

    async def _on_event(self, event: ReActEvent, data: Any = None) -> None:
        """Handle framework events — stream to WebSocket and persist incrementally."""
        event_value: str = event.value

        if event_value == _MODEL_REASONING:
            text: str = data
            evt = ModelReasoningDelta(
                session_id=self._session_id,
                agent_name=self._agent_name,
                text=text,
                turn_id=self._current_turn_id,
                segment_id="_reasoning",
            )
            await self._persist_partial(evt)
            await self._send_event(evt)
            self._ensure_turn_started()
            self._accumulate_segment(text, "reasoning", None)

        elif event_value == _TOOL_CALL_START:
            tool_name: str = data.tool_name
            full_args: dict[str, object] = data.arguments or {}
            # The tool node canonicalizes call_id before emitting (assigning
            # one when the provider omits it), so the id here is the SAME id
            # the later END will carry — pass it through verbatim.
            call_id: str = data.call_id

            await self._flush_active_segment()
            self._ensure_turn_started()
            # NOTE: the ToolCallEvent is persisted together with its
            # ToolResultEvent in the TOOL_CALL_END branch below -- NOT here.
            # Persisting the call on START leaves orphan tool_call events when
            # the turn suspends for approval before the tool runs; and on resume
            # the tool node emits ONLY TOOL_CALL_END (the call was already
            # decided in the suspended snapshot), so the call would otherwise
            # land in a different turn / never pair with its result, and the
            # materializer would drop it -> "no tool rendering after refresh".
            evt = ToolCallStartEvent(
                session_id=self._session_id,
                agent_name=self._agent_name,
                tool=tool_name,
                args=_truncate_tool_args(full_args),
                turn_id=self._current_turn_id,
                call_id=call_id,
            )
            await self._send_event(evt)

        elif event_value == _TOOL_CALL_END:
            await self._flush_active_segment()
            payload: ToolCallEndPayload = data
            tc = payload.tool_call
            tool_result = payload.result
            seq = payload.seq
            tool_name: str = tc.tool_name
            raw_error: str | None = tool_result.error
            full_result: str = tool_result.message_content()
            # The canonical id assigned by the tool node — shared by the
            # persisted pair AND the streamed END, equal to the START's id.
            end_call_id: str | None = tc.call_id

            if self._transcript_store is not None:
                self._ensure_turn_started()
                full_args = tc.arguments or {}
                # Persist call + result TOGETHER so they share a turn_id and
                # the materializer pairs them into one complete tool block.
                # This is also the ONLY persistence point on a resumed approval
                # turn (no preceding TOOL_CALL_START), so it must carry the
                # call args -- otherwise the resumed tool renders result-only.
                tc_evt = TcEvent(
                    session_id=self._session_id,
                    agent_name=self._agent_name,
                    turn_id=self._current_turn_id,
                    call_id=end_call_id,
                    tool_name=tool_name,
                    args=full_args,
                )
                await self._persist(tc_evt)
                tr_evt = TrEvent(
                    session_id=self._session_id,
                    agent_name=self._agent_name,
                    turn_id=self._current_turn_id,
                    call_id=end_call_id,
                    tool_name=tool_name,
                    result=full_result.strip(),
                    error=raw_error,
                    seq=seq,
                )
                await self._persist(tr_evt)

            result_summary: str = (
                full_result[:_MAX_TOOL_RESULT_LEN] + "..."
                if len(full_result) > _MAX_TOOL_RESULT_LEN
                else full_result
            )
            evt = ToolCallEndEvent(
                session_id=self._session_id,
                agent_name=self._agent_name,
                tool=tool_name,
                result_summary=result_summary,
                turn_id=self._current_turn_id,
                call_id=end_call_id,
                seq=seq,
            )
            await self._send_event(evt)

        else:
            await super()._on_event(event, data)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
