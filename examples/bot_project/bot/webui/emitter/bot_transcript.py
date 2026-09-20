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

Projections receive full-fidelity facts (full tool args, full result,
``seq``, ``part_id``) — display truncation is each projection's own
concern.
"""

from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

from modex_agent.adapters.emitter import StreamingAwareEmitter
from modex_agent.adapters.output import OutputAdapter
from modex_agent.agents.react.agent import ReActEvent
from modex_agent.agents.react.constants import ToolCallEndPayload
from modex_agent.core.emitter import AgentResult
from modex_agent.core.events import EmitterConfig
from modex_agent.core.session_id import agent_of
from modex_agent.core.turn_events import (
    TurnEvent,
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
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


class BotTranscriptEmitter(StreamingAwareEmitter[ReActEvent], ABC):
    """Shared turn-record lifecycle with two concrete projections.

    This base owns recording (transcript writes, segment buffering, turn
    identity); subclasses own projection — translating each recorded fact
    into their own sink format. The five ``_project_*`` hooks receive the
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

        # Incremental turn state — multiple segments tracked by part_id.
        # Each part_id accumulates independently so token-level interleaving
        # (text part_1 and reasoning part_2 alternating) produces exactly 2
        # transcript events, not hundreds. Projections still fire per-token
        # (true streaming).
        self._segments: dict[str, str] = {}
        self._segment_kinds: dict[str, str] = {}
        self._segment_order: list[str] = []
        self._current_turn_id: str = ""
        self._turn_active: bool = False
        self._turn_started_at: float = time.time()
        self._pending_external_tools: dict[str, tuple[str, dict[str, object]]] = {}

    # ------------------------------------------------------------------
    # Projection contract (subclass sink formats)
    # ------------------------------------------------------------------

    @abstractmethod
    async def _project_text_delta(
        self, text: str, part_id: str | None
    ) -> None:
        """Project one content delta (full text fragment, ``part_id`` if the
        source stream identifies output parts)."""

    @abstractmethod
    async def _project_reasoning_delta(
        self, text: str, part_id: str | None
    ) -> None:
        """Project one reasoning delta."""

    @abstractmethod
    async def _project_tool_start(
        self,
        tool_name: str,
        call_id: str,
        full_args: dict[str, Any],  # open extension payload — tool schemas evolve
    ) -> None:
        """Project a tool call start with the FULL argument dict."""

    @abstractmethod
    async def _project_tool_end(
        self,
        tool_name: str,
        call_id: str | None,
        full_result: str,
        seq: int | None,
    ) -> None:
        """Project a tool call end with the FULL result text and ``seq``."""

    @abstractmethod
    async def _project_turn_end(self, latency_ms: int) -> None:
        """Project turn completion (``latency_ms`` since turn start)."""

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

    def _ensure_turn_started(self) -> None:
        """Lazily start a new turn with UUID turn_id.

        Projections may render turn boundaries for their sink; the turn start
        itself is NOT persisted to the transcript store because it carries no
        conversational content.
        """
        if self._turn_active:
            return
        self._current_turn_id = uuid.uuid4().hex[:12]
        self._turn_active = True
        self._turn_started_at = time.time()

    def _accumulate_segment(self, text: str, kind: str, part_id: str | None) -> None:
        if not text:
            return
        self._ensure_turn_started()
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
    # Content entry points (single-write recording + projection)
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
        await self._record_text_delta(delta, None)

    async def emit_stream_end(self, resuming: bool = False) -> None:
        await self._flush_active_segment()

    async def emit_turn_event(self, event: TurnEvent) -> None:
        match event:
            case TurnTextEvent(text=text, part_id=part_id):
                self._accumulate_segment(text, "text", part_id)
                await self._record_text_delta(text, part_id)
            case TurnReasoningEvent(text=text, part_id=part_id):
                self._ensure_turn_started()
                self._accumulate_segment(text, "reasoning", part_id)
                await self._record_reasoning_delta(text, part_id)
            case TurnToolCallEvent(
                tool_name=tool_name, call_id=call_id, arguments=arguments
            ):
                await self._flush_active_segment()
                self._ensure_turn_started()
                full_args: dict[str, object] = dict(arguments)
                self._pending_external_tools[call_id] = (tool_name, full_args)
                await self._project_tool_start(tool_name, call_id, full_args)
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
                await self._project_tool_end(tool_name, call_id, output, None)

    async def emit_complete(self, result: AgentResult) -> None:
        try:
            await self._flush_active_segment()
            await super().emit_complete(result)
            latency_ms: int = int((time.time() - self._turn_started_at) * 1000)
            await self._project_turn_end(latency_ms)
        finally:
            await self._clear_partial()
            self._segments = {}
            self._segment_kinds = {}
            self._segment_order = []
            self._pending_external_tools = {}
            self._turn_active = False
            self._turn_started_at = time.time()

    async def _on_event(self, event: ReActEvent, data: Any = None) -> None:
        """Handle framework events — record + project each one."""
        event_value: str = event.value

        if event_value == "model_reasoning":
            text: str = data
            await self._record_reasoning_delta(text, None)
            self._ensure_turn_started()
            self._accumulate_segment(text, "reasoning", None)

        elif event_value == "tool_call_start":
            tool_name: str = data.tool_name
            full_args: dict[str, object] = data.arguments or {}
            # The tool node canonicalizes call_id before emitting (assigning
            # one when the provider omits it), so the id here is the SAME id
            # the later END will carry — pass it through verbatim.
            call_id: str = data.call_id

            await self._flush_active_segment()
            self._ensure_turn_started()
            # NOTE: the ToolCallEvent is persisted together with its
            # ToolResultEvent in the tool_call_end branch below -- NOT here.
            # Persisting the call on START leaves orphan tool_call events when
            # the turn suspends for approval before the tool runs; and on resume
            # the tool node emits ONLY TOOL_CALL_END (the call was already
            # decided in the suspended snapshot), so the call would otherwise
            # land in a different turn / never pair with its result, and the
            # materializer would drop it -> "no tool rendering after refresh".
            await self._project_tool_start(tool_name, call_id, full_args)

        elif event_value == "tool_call_end":
            await self._flush_active_segment()
            payload: ToolCallEndPayload = data
            tc = payload.tool_call
            tool_result = payload.result
            seq = payload.seq
            tool_name = tc.tool_name
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

            await self._project_tool_end(tool_name, end_call_id, full_result, seq)

        else:
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
