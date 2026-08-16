"""Segment accumulation / flush / send helpers for ``WebBotEmitter``.

Holds the per-turn segment buffer logic (text + reasoning parts accumulated
independently by ``part_id`` and flushed as a single transcript event per
segment), the WebSocket envelope sender, the tool-arg truncation helper, the
default session-meta resolver, and the ReActEvent value / truncation
constants. Extracted verbatim from the original ``emitter.py``; emission
behaviour is unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..events import (
    AssistantReasoningEvent,
    AssistantTextEvent,
    DeltaEnvelope,
    ServerEvent,
    SessionMeta,
)

if TYPE_CHECKING:
    from .web_bot import WebBotEmitter

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
    """Default resolver: no parent session known."""
    return SessionMeta()


def _accumulate_segment(
    self: WebBotEmitter, text: str, kind: str, part_id: str | None
) -> None:
    if not text:
        return
    self._ensure_turn_started()
    key = part_id if part_id else f"_{kind}"
    if key not in self._segments:
        self._segments[key] = ""
        self._segment_kinds[key] = kind
        self._segment_order.append(key)
    self._segments[key] += text


async def _flush_active_segment(self: WebBotEmitter) -> None:
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


async def _send_event(self: WebBotEmitter, event: ServerEvent) -> None:
    """Wrap *event* in a structured DeltaEnvelope and enqueue it."""
    parent_meta = self._session_meta_resolver()
    envelope = DeltaEnvelope.from_event(
        event,
        metadata=self._metadata(),
        pool=self._pool or "",
        parent_session_id=parent_meta.parent_session_id,
    )
    await self._output.send_envelope(envelope)
