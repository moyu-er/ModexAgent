"""WebBotEmitter — WebUI WebSocket projection of the bot transcript emitter.

Streaming phase:
  - ``emit_delta`` / ``_on_event`` → push incremental JSON events via WebSocket.
  - Deltas are NOT persisted individually — they are transient UI updates.

Recording lifecycle (segments, transcript persistence, partial buffers, turn
identity) lives on :class:`BotTranscriptEmitter`; this subclass only projects
each recorded fact into WebUI ``ServerEvent``s. Full data is saved in the
transcript store by the base; only truncated versions are pushed to the
frontend for rendering.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from modex_agent.core.events import EmitterConfig

from ...adapters.web_socket import WebSocketOutputAdapter
from ..events import (
    DeltaEnvelope,
    ModelContentDelta,
    ModelReasoningDelta,
    ServerEvent,
    SessionMeta,
    ToolCallEndEvent,
    ToolCallStartEvent,
    TurnEndEvent,
)
from ..transcript_store import TranscriptStore
from .bot_transcript import BotTranscriptEmitter

# ── Truncation limits for WebSocket events ─────────────────────────────────

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


class WebBotEmitter(BotTranscriptEmitter):
    """WebUI WebSocket projection — sends incremental deltas via the
    WebSocket output adapter, records the canonical transcript via the
    shared base lifecycle.
    """

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
        super().__init__(
            output_adapter,
            session_id,
            config,
            send_timeout=send_timeout,
            pool=pool,
            transcript_store=transcript_store,
            session_meta_resolver=session_meta_resolver,
            sessions_dir_provider=sessions_dir_provider,
        )
        self._output: WebSocketOutputAdapter = output_adapter

    # ------------------------------------------------------------------
    # WebSocket projection
    # ------------------------------------------------------------------

    async def _send_event(self, event: ServerEvent) -> None:
        """Wrap *event* in a structured DeltaEnvelope and enqueue it."""
        parent_meta = self._session_meta_resolver()
        envelope = DeltaEnvelope.from_event(
            event,
            metadata=self._metadata(),
            pool=self._pool or "",
            parent_session_id=parent_meta.parent_session_id,
        )
        await self._output.send_envelope(envelope)

    async def _project_text_delta(self, text: str, part_id: str | None) -> None:
        evt = ModelContentDelta(
            session_id=self._session_id,
            agent_name=self._agent_name,
            text=text,
            turn_id=self._current_turn_id,
            segment_id=part_id if part_id else "_text",
        )
        await self._send_event(evt)

    async def _project_reasoning_delta(self, text: str, part_id: str | None) -> None:
        evt = ModelReasoningDelta(
            session_id=self._session_id,
            agent_name=self._agent_name,
            text=text,
            turn_id=self._current_turn_id,
            segment_id=part_id if part_id else "_reasoning",
        )
        await self._send_event(evt)

    async def _project_tool_start(
        self,
        tool_name: str,
        call_id: str,
        full_args: dict[str, Any],
    ) -> None:
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

    async def _project_tool_end(
        self,
        tool_name: str,
        call_id: str | None,
        full_result: str,
        seq: int | None,
    ) -> None:
        result_summary: str = (
            full_result[:_MAX_TOOL_RESULT_LEN] + "..."
            if len(full_result) > _MAX_TOOL_RESULT_LEN
            else full_result
        )
        await self._send_event(
            ToolCallEndEvent(
                session_id=self._session_id,
                agent_name=self._agent_name,
                tool=tool_name,
                result_summary=result_summary,
                turn_id=self._current_turn_id,
                call_id=call_id,
                seq=seq,
            )
        )

    async def _project_turn_end(self, latency_ms: int) -> None:
        await self._send_event(
            TurnEndEvent(
                session_id=self._session_id,
                agent_name=self._agent_name,
                turn_id=self._current_turn_id if self._turn_active else "",
                latency_ms=latency_ms,
            )
        )
