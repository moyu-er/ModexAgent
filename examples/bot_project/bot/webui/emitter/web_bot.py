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

import time
from collections.abc import Callable
from dataclasses import dataclass
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
    ToolArgsDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    TurnEndEvent,
)
from ..transcript_store import TranscriptStore
from .bot_transcript import BotTranscriptEmitter

# ── Truncation limits for WebSocket events ─────────────────────────────────

_MAX_TOOL_ARGS_LEN: int = 500
_MAX_TOOL_RESULT_LEN: int = 200

# ── tool_args_delta 节流外发参数 ────────────────────────────────────────────

_TOOL_ARGS_THROTTLE_SECONDS: float = 0.1
_TOOL_ARGS_PREVIEW_CHARS: int = 200


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


@dataclass
class _ToolArgsStreamState:
    """一个 call_id 的参数流累积账目(节流外发用, 模块内部值对象)。"""
    chars: int = 0
    preview: str = ""
    last_sent: float | None = None  # time.monotonic(); None = 尚未外发过


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
        # tool_args_delta 节流账目, 按 call_id 键控; 由 _project_tool_start /
        # _project_turn_end 清场(见各自清理注释)。
        self._args_stream_state: dict[str, _ToolArgsStreamState] = {}

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

    async def _project_tool_args_delta(self, tool_name: str, call_id: str, args_fragment: str) -> None:
        state = self._args_stream_state.setdefault(call_id, _ToolArgsStreamState())
        state.chars += len(args_fragment)
        state.preview = (state.preview + args_fragment)[-_TOOL_ARGS_PREVIEW_CHARS:]
        # leading-edge 节流: 每个 call_id 的首 fragment 立即外发; 窗口内的
        # 后续 fragment 吞掉, 下一窗口的外发携带累积 chars + 尾部 preview。
        # 无需 trailing-edge 定时器 —— tool_call_start 随后携带全量参数, 兜底
        # 一切预热态。
        now = time.monotonic()
        if (
            state.last_sent is None
            or (now - state.last_sent) >= _TOOL_ARGS_THROTTLE_SECONDS
        ):
            await self._send_event(
                ToolArgsDeltaEvent(
                    session_id=self._session_id,
                    agent_name=self._agent_name,
                    tool=tool_name,
                    call_id=call_id,
                    turn_id=self._current_turn_id,
                    chars=state.chars,
                    preview=state.preview,
                )
            )
            state.last_sent = now

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
        # 预热账目用完即弃: tool_call_start 已携带全量参数, 该 call_id 的累积
        # 状态不再需要。provider 省略 id 时规范 call_id 可能与流式 id 不同,
        # pop 尽力而为, 残项由 _project_turn_end 清场兜底。
        self._args_stream_state.pop(call_id, None)

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
        # 清场: LENGTH 截断 / 规范 id 不一致等孤儿预热账目随回合结束一并
        # 丢弃, 避免跨回合泄漏累积状态。
        self._args_stream_state.clear()
