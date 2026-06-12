"""WebBotEmitter — streaming event emitter for WebUI WebSocket sessions.

Streaming phase:
  - ``emit_delta`` / ``_on_event`` → push incremental JSON events via WebSocket.
  - Deltas are NOT persisted — they are transient UI updates.

Turn complete:
  - ``emit_complete`` → persist the FULL turn (content + reasoning + tools)
    to the transcript store, THEN send ``turn_end`` to the WebSocket client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Generic, TypeVar

from framework.agents.react.agent import ReActEvent
from framework.core.emitter import AgentResult, ContentEmitter, EmitterConfig, StreamingAwareEmitter
from framework.pipeline.adapters import OutputAdapter

from .events import (
    AssistantTurnEvent,
    ModelContentDelta,
    ModelReasoningDelta,
    ServerEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    TurnEndEvent,
)
from .transcript_store import TranscriptStore

# ── ReActEvent values we handle ────────────────────────────────────────────

_MODEL_REASONING: str = "model_reasoning"
_TOOL_CALL_START: str = "tool_call_start"
_TOOL_CALL_END: str = "tool_call_end"

# ── Block merging ──────────────────────────────────────────────────────────


def _merge_blocks(blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    """Merge adjacent same-kind blocks so the transcript is compact.

    ``emit_delta`` appends one block per chunk, so a sentence like
    ``"Hello World"`` arrives as ``[{text:"Hello"}, {text:" World"}]``.
    Without merging, the frontend renders many tiny blocks stacked
    vertically, producing narrow/elongated message bubbles.
    """
    if not blocks:
        return blocks
    merged: list[dict[str, object]] = [dict(blocks[0])]
    for block in blocks[1:]:
        prev = merged[-1]
        kind = block.get("kind")
        if kind in ("text", "reasoning") and prev.get("kind") == kind:
            prev["text"] = str(prev.get("text", "")) + str(block.get("text", ""))
        else:
            merged.append(dict(block))
    return merged


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


class WebBotEmitter(StreamingAwareEmitter[ReActEvent]):
    """Streaming emitter for WebUI.

    - Sends incremental deltas via WebSocket for real-time UI rendering.
    - Collects full content / reasoning / tool traces during the turn.
    - At ``emit_complete``, persists the complete turn to the transcript
      store and notifies the client via ``turn_end``.
    """

    def __init__(
        self,
        output_adapter: OutputAdapter,
        session_id: str,
        config: EmitterConfig | None = None,
        *,
        send_timeout: float | None = None,
        transcript_store: TranscriptStore | None = None,
    ) -> None:
        super().__init__(output_adapter, session_id, config, send_timeout=send_timeout)
        # session_id format: {conv_id}.{agent_name}[.{invocation_id}]
        parts = session_id.split(".", 2)
        self._conversation_id: str = parts[0]
        self._agent_name: str = parts[1] if len(parts) > 1 else "main"
        self._invocation_id: str | None = parts[2] if len(parts) > 2 else None
        self._turn_counter: int = 1
        self._transcript_store: TranscriptStore | None = transcript_store

        # Ordered blocks — built during streaming, persisted at turn end.
        self._blocks: list[dict[str, object]] = []
        self._turn_started_at: float = time.time()

    # ------------------------------------------------------------------
    # Streaming (transient — WebSocket only, no persistence)
    # ------------------------------------------------------------------

    async def emit_delta(self, delta: str) -> None:
        """Push a content chunk to the WebSocket client and record a text block."""
        if not delta:
            return
        self._blocks.append({"kind": "text", "text": delta})
        evt = ModelContentDelta(
            conversation_id=self._conversation_id,
            agent_name=self._agent_name,
            text=delta,
            turn_id=self._current_turn_id(),
        )
        await self._send_event(evt)

    # ------------------------------------------------------------------
    # Turn complete (persist + notify)
    # ------------------------------------------------------------------

    async def emit_complete(self, result: AgentResult) -> None:
        """Persist the complete turn, then notify the client."""
        await super().emit_complete(result)

        latency_ms = int((time.time() - self._turn_started_at) * 1000)

        # Save the complete turn to transcript (if store configured).
        if self._transcript_store is not None:
            turn_record = AssistantTurnEvent(
                conversation_id=self._conversation_id,
                agent_name=self._agent_name,
                blocks=_merge_blocks(self._blocks),
                turn_id=self._current_turn_id(),
                latency_ms=latency_ms,
            )
            self._transcript_store.append(
                self._conversation_id,
                self._agent_name,
                turn_record,
            )

        # Notify frontend that the turn is complete.
        turn_end = TurnEndEvent(
            conversation_id=self._conversation_id,
            agent_name=self._agent_name,
            turn_id=self._current_turn_id(),
            latency_ms=latency_ms,
        )
        await self._send_event(turn_end)

        # Reset for next turn.
        self._blocks.clear()
        self._turn_started_at = time.time()
        self._turn_counter += 1

    async def emit_error(self, error: str) -> None:
        """Notify that an error occurred, then delegate to parent."""
        await super().emit_error(error)

    # ------------------------------------------------------------------
    # Event dispatch (ReActEvent → ServerEvent)
    # ------------------------------------------------------------------

    async def _on_event(self, event: ReActEvent, data: Any = None) -> None:
        """Handle framework events — stream to WebSocket and buffer."""
        event_value: str = event.value

        if event_value == _MODEL_REASONING:
            text: str = data
            self._blocks.append({"kind": "reasoning", "text": text})
            evt = ModelReasoningDelta(
                conversation_id=self._conversation_id,
                agent_name=self._agent_name,
                text=text,
                turn_id=self._current_turn_id(),
            )
            await self._send_event(evt)

        elif event_value == _TOOL_CALL_START:
            tool_name: str = getattr(data, "tool_name", str(data or ""))
            full_args: dict[str, object] = getattr(data, "arguments", {}) or {}
            self._blocks.append({"kind": "tool", "tool": tool_name, "args": full_args})
            evt = ToolCallStartEvent(
                conversation_id=self._conversation_id,
                agent_name=self._agent_name,
                tool=tool_name,
                args=_truncate_tool_args(full_args),
                turn_id=self._current_turn_id(),
            )
            await self._send_event(evt)

        elif event_value == _TOOL_CALL_END:
            tc, tool_result = data  # framework sends (ToolCall, ToolResult) tuple
            tool_name = tc.tool_name
            # Extract the actual content from ToolResult, not its repr.
            raw = getattr(tool_result, "result", None)
            err = getattr(tool_result, "error", None)
            if err:
                full_result = f"Error: {err}"
            elif raw is not None:
                full_result = str(raw)
            else:
                full_result = ""
            # Store FULL result for transcript; send TRUNCATED to frontend.
            result_summary: str = (
                full_result[:_MAX_TOOL_RESULT_LEN] + "..."
                if len(full_result) > _MAX_TOOL_RESULT_LEN
                else full_result
            )
            for block in reversed(self._blocks):
                if block.get("kind") == "tool" and block.get("tool") == tool_name:
                    block["result"] = full_result
                    break
            evt = ToolCallEndEvent(
                conversation_id=self._conversation_id,
                agent_name=self._agent_name,
                tool=tool_name,
                result_summary=result_summary,
                turn_id=self._current_turn_id(),
            )
            await self._send_event(evt)

        else:
            await super()._on_event(event, data)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _current_turn_id(self) -> str:
        """Return the current turn identifier string (e.g. ``"turn_1"``)."""
        return f"turn_{self._turn_counter}"

    async def _send_event(self, event: ServerEvent) -> None:
        """Serialize *event* to JSON and send via the output adapter."""
        json_str = json.dumps(event.to_dict())
        await self.output_adapter.send_delta(json_str, self.session_id)


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
