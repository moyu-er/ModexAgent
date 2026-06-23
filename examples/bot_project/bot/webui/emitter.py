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

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Generic, TypeVar

from framework.agents.react.agent import ReActEvent
from framework.core.emitter import AgentResult, ContentEmitter, EmitterConfig, StreamingAwareEmitter
from framework.core.session_id import agent_of
from framework.core.tool_manager import ToolResult
from framework.core.types import ToolCall

from ..adapters.web_socket import WebSocketOutputAdapter
from .events import (
    AssistantReasoningEvent,
    AssistantTextEvent,
    DeltaEnvelope,
    ModelContentDelta,
    ModelReasoningDelta,
    ServerEvent,
    SessionMeta,
    ToolCallEndEvent,
    ToolCallEvent as TcEvent,
    ToolCallStartEvent,
    ToolResultEvent as TrEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from .transcript_store import TranscriptStore

# ── ReActEvent values we handle ────────────────────────────────────────────

_MODEL_REASONING: str = "model_reasoning"
_TOOL_CALL_START: str = "tool_call_start"
_TOOL_CALL_END: str = "tool_call_end"

# ── Truncation limits for WebSocket events ─────────────────────────────────
# Full data is saved in the transcript store; only truncated versions are
# pushed to the frontend for rendering.

_MAX_TOOL_ARGS_LEN: int = 500
_MAX_TOOL_RESULT_LEN: int = 200


def _truncate_tool_args(args: dict[str, object]) -> dict[str, object]:
    """Return a copy of *args* with values truncated for frontend display."""
    truncated: dict[str, object] = {}
    for key, val in args.items():
        s = str(val)
        if len(s) > _MAX_TOOL_ARGS_LEN:
            truncated[key] = s[:_MAX_TOOL_ARGS_LEN] + "..."
        else:
            truncated[key] = val
    return truncated

# ── Emitter ────────────────────────────────────────────────────────────────


def _empty_session_meta() -> SessionMeta:
    """Default resolver: no business routing context known."""
    return SessionMeta()


class WebBotEmitter(StreamingAwareEmitter[ReActEvent]):
    """Streaming emitter for WebUI.

    - Sends incremental deltas via WebSocket for real-time UI rendering.
    - Collects full content / reasoning / tool traces during the turn.
    - At ``emit_complete``, persists the complete turn to the transcript
      store and notifies the client via ``turn_end``.
    """

    def __init__(
        self,
        output_adapter: WebSocketOutputAdapter,
        session_id: str,
        config: EmitterConfig | None = None,
        *,
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
        self._turn_counter: int = 1
        self._transcript_store: TranscriptStore | None = transcript_store
        # Lazy resolver for business routing context (pool, parent_session_id).
        # Read at send time so it reflects the latest pool map / parent
        # registry (populated by WebUIService after pool init).
        self._session_meta_resolver = session_meta_resolver or _empty_session_meta
        # Resolver-cell-driven workspace resolution for transcript writes. When
        # set, the owning workspace's sessions_dir is resolved per write from
        # the per-workspace resolver cell (same source memory uses) — this
        # survives the broker-queue task boundary where the bind_workspace_root
        # ContextVar is lost. None = fall back to the ctxvar (legacy/tests).
        self._sessions_dir_provider: Callable[[], Path | None] | None = (
            sessions_dir_provider
        )

        # Incremental turn state — text is buffered and flushed at stream/turn
        # boundaries so both the streaming and non-streaming agent paths persist
        # exactly one ``AssistantTextEvent`` per LLM response.
        self._text_buffer: str = ""
        self._current_turn_id: str = ""
        self._turn_active: bool = False
        self._tool_seq: int = 0
        self._turn_started_at: float = time.time()

    # ------------------------------------------------------------------
    # Turn lifecycle helpers
    # ------------------------------------------------------------------

    def _persist(self, event: ServerEvent) -> None:
        """Append *event* to the transcript store under the owning workspace.

        The sessions_dir is resolved from the resolver-cell provider when wired
        (correct workspace even inside the broker consumer task, where the
        bind_workspace_root ContextVar is lost); when None the store uses the
        standard ctxvar/ctor fallback (backward compat / tests).
        """
        if self._transcript_store is None:
            return
        sessions_dir = (
            self._sessions_dir_provider() if self._sessions_dir_provider else None
        )
        # Only pass sessions_dir explicitly when non-None — JSONLTranscriptStore
        # (used in tests / standalone paths) does not accept the kwarg, so the
        # non-resolver path keeps the 2-arg call the ABC defines.
        if sessions_dir is not None:
            self._transcript_store.append(
                self._session_id, event, sessions_dir=sessions_dir
            )
        else:
            self._transcript_store.append(self._session_id, event)

    def _resolve_call_id(self, raw_call_id: str | None, tool_name: str) -> str:
        """Return a stable call_id, falling back to monotonic counter when None."""
        if raw_call_id is not None:
            return raw_call_id
        self._tool_seq += 1
        return f"{tool_name}_{self._tool_seq}"

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
        self._tool_seq = 0
        self._turn_started_at = time.time()

    # ------------------------------------------------------------------
    # Streaming (transient WS deltas + incremental persistence)
    # ------------------------------------------------------------------

    async def emit_content(self, full_content: str) -> None:
        """Buffer clean LLM content; flushed to TranscriptStore at stream end."""
        self._ensure_turn_started()
        text: str = full_content.strip()
        if text:
            self._text_buffer += text

    async def emit_delta(self, delta: str) -> None:
        """Push a content chunk to the WebSocket client."""
        if not delta:
            return
        self._ensure_turn_started()
        self._text_buffer += delta
        evt = ModelContentDelta(
            session_id=self._session_id,
            agent_name=self._agent_name,
            text=delta,
            turn_id=self._current_turn_id,
        )
        await self._send_event(evt)

    async def emit_stream_end(self, resuming: bool = False) -> None:
        """Flush buffered text to the transcript store.

        Called by the agent after each LLM response (both the plain-streaming
        and the control-interceptor streaming paths).  ``resuming`` only tells
        the agent whether tool calls follow; the emitter always persists the
        text accumulated so far.
        """
        await self._flush_text_buffer()

    # ------------------------------------------------------------------
    # Turn complete (flush + notify)
    # ------------------------------------------------------------------

    async def emit_complete(self, result: AgentResult) -> None:
        await self._flush_text_buffer()
        await super().emit_complete(result)
        latency_ms: int = int((time.time() - self._turn_started_at) * 1000)

        # ``TurnEndEvent`` is sent to the WebSocket so the frontend can
        # mark the turn as finished. It is NOT persisted — like
        # ``TurnStartEvent`` it carries no conversational content.
        ws_turn_end = TurnEndEvent(
            session_id=self._session_id,
            agent_name=self._agent_name,
            turn_id=self._current_turn_id if self._turn_active else "",
            latency_ms=latency_ms,
        )
        await self._send_event(ws_turn_end)

        self._text_buffer = ""
        self._turn_active = False
        self._turn_started_at = time.time()
        self._turn_counter += 1

    async def emit_error(self, error: str) -> None:
        """Notify that an error occurred, then delegate to parent."""
        await super().emit_error(error)

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
            )
            await self._send_event(evt)

            if self._transcript_store is not None:
                self._ensure_turn_started()
                reasoning_evt = AssistantReasoningEvent(
                    session_id=self._session_id,
                    agent_name=self._agent_name,
                    turn_id=self._current_turn_id,
                    text=text,
                )
                self._persist(reasoning_evt)

        elif event_value == _TOOL_CALL_START:
            tool_name: str = data.tool_name
            full_args: dict[str, object] = data.arguments or {}

            if self._transcript_store is not None:
                self._ensure_turn_started()
                call_id: str = self._resolve_call_id(data.call_id, tool_name)
                tc_evt = TcEvent(
                    session_id=self._session_id,
                    agent_name=self._agent_name,
                    turn_id=self._current_turn_id,
                    call_id=call_id,
                    tool_name=tool_name,
                    args=full_args,
                )
                self._persist(tc_evt)

            evt = ToolCallStartEvent(
                session_id=self._session_id,
                agent_name=self._agent_name,
                tool=tool_name,
                args=_truncate_tool_args(full_args),
                turn_id=self._current_turn_id,
            )
            await self._send_event(evt)

        elif event_value == _TOOL_CALL_END:
            tc, tool_result = data
            tool_name: str = tc.tool_name
            raw_result: str | None = tool_result.result
            raw_error: str | None = tool_result.error
            if raw_error:
                full_result: str = f"Error: {raw_error}"
            elif raw_result is not None:
                full_result = str(raw_result)
            else:
                full_result = ""

            if self._transcript_store is not None:
                call_id: str = self._resolve_call_id(tc.call_id, tool_name)
                tr_evt = TrEvent(
                    session_id=self._session_id,
                    agent_name=self._agent_name,
                    turn_id=self._current_turn_id,
                    call_id=call_id,
                    tool_name=tool_name,
                    result=full_result.strip(),
                    error=raw_error,
                )
                self._persist(tr_evt)

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
            )
            await self._send_event(evt)

        else:
            await super()._on_event(event, data)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _flush_text_buffer(self) -> None:
        """Persist accumulated LLM text as a single ``AssistantTextEvent``."""
        if self._transcript_store is None:
            self._text_buffer = ""
            return
        text: str = self._text_buffer.strip()
        if not text:
            return
        evt = AssistantTextEvent(
            session_id=self._session_id,
            agent_name=self._agent_name,
            turn_id=self._current_turn_id,
            text=text,
        )
        self._persist(evt)
        self._text_buffer = ""

    async def _send_event(self, event: ServerEvent) -> None:
        """Wrap *event* in a structured DeltaEnvelope and enqueue it."""
        meta = self._session_meta_resolver()
        envelope = DeltaEnvelope.from_event(
            event,
            metadata=self._metadata(),
            pool=meta.pool,
            parent_session_id=meta.parent_session_id,
        )
        await self._output.send_envelope(envelope)

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


# ── Composite (fan-out) emitter ──────────────────────────────────────────────

logger = logging.getLogger(__name__)

_E = TypeVar("_E")


class CompositeEmitter(ContentEmitter[_E], Generic[_E]):
    """Fan-out emitter that delegates all calls to a list of child emitters.

    Each method is forwarded to ALL children concurrently via
    ``asyncio.gather(..., return_exceptions=True)``.  Errors in individual
    children are logged but do not prevent other children from receiving the
    event.

    Usage::

        emitter = CompositeEmitter([
            WebBotEmitter(ws_output, session_id, ...),
            QQBotEmitter(qq_output, session_id, ...),
        ])
    """

    def __init__(
        self,
        emitters: list[ContentEmitter[_E]],
        config: EmitterConfig | None = None,
    ) -> None:
        super().__init__(config)
        self._emitters: list[ContentEmitter[_E]] = list(emitters)

    @property
    def emitters(self) -> list[ContentEmitter[_E]]:
        return list(self._emitters)

    def set_sessions_dir_provider(
        self, provider: Callable[[], Path | None] | None
    ) -> None:
        """Forward the sessions_dir provider to every child emitter that accepts one."""
        for child in self._emitters:
            setter = getattr(child, "set_sessions_dir_provider", None)
            if setter is not None:
                setter(provider)

    def wants_streaming(self) -> bool:
        """Return ``True`` if ANY child wants streaming."""
        return any(e.wants_streaming() for e in self._emitters)

    async def emit(self, event: _E, data: Any = None) -> None:
        results = await asyncio.gather(
            *(e.emit(event, data) for e in self._emitters),
            return_exceptions=True,
        )
        self._log_exceptions(results, "emit")

    async def emit_delta(self, delta: str) -> None:
        results = await asyncio.gather(
            *(e.emit_delta(delta) for e in self._emitters),
            return_exceptions=True,
        )
        self._log_exceptions(results, "emit_delta")

    async def emit_content(self, full_content: str) -> None:
        results = await asyncio.gather(
            *(e.emit_content(full_content) for e in self._emitters),
            return_exceptions=True,
        )
        self._log_exceptions(results, "emit_content")

    async def emit_stream_end(self, resuming: bool = False) -> None:
        results = await asyncio.gather(
            *(e.emit_stream_end(resuming) for e in self._emitters),
            return_exceptions=True,
        )
        self._log_exceptions(results, "emit_stream_end")

    async def emit_complete(self, result: AgentResult) -> None:
        results = await asyncio.gather(
            *(e.emit_complete(result) for e in self._emitters),
            return_exceptions=True,
        )
        self._log_exceptions(results, "emit_complete")

    async def emit_error(self, error: str) -> None:
        results = await asyncio.gather(
            *(e.emit_error(error) for e in self._emitters),
            return_exceptions=True,
        )
        self._log_exceptions(results, "emit_error")

    async def flush(self) -> None:
        results = await asyncio.gather(
            *(e.flush() for e in self._emitters),
            return_exceptions=True,
        )
        self._log_exceptions(results, "flush")

    @staticmethod
    def _log_exceptions(results: list[object], method: str) -> None:
        """Log any exceptions from ``asyncio.gather(return_exceptions=True)``."""
        for exc in results:
            if isinstance(exc, Exception):
                logger.error("CompositeEmitter.%s child error: %s", method, exc)
