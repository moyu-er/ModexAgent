"""ProgressReportHook — 各 hook 点推送事件到 ControlEventBus。"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from framework.control.types import (
    ControlEvent,
    ControlEventType,
    ControlScope,
)
from framework.core.agent import AgentContext

if TYPE_CHECKING:
    from framework.control.event_bus import ControlEventBus

logger = logging.getLogger(__name__)


def _get_iteration(ctx: AgentContext[Any]) -> int:
    state = getattr(ctx.runtime, "state", None) if ctx.runtime else None
    return getattr(state, "iteration", 0)


class ProgressReportHook:
    """推送进度事件到 ControlEventBus。

    在各 hook 点发射 AGENT_PROGRESS 事件，监控/仪表板可订阅。
    """

    def __init__(self, event_bus: ControlEventBus) -> None:
        self._event_bus = event_bus

    async def before_iteration(self, ctx: AgentContext[Any]) -> None:
        iteration = _get_iteration(ctx)
        await self._emit(ctx, {"phase": "iteration_start", "iteration": iteration})

    async def after_iteration(self, ctx: AgentContext[Any]) -> None:
        iteration = _get_iteration(ctx)
        await self._emit(ctx, {"phase": "iteration_end", "iteration": iteration})

    async def before_tool_execution(
        self, ctx: AgentContext[Any], tool_calls: list[Any],
    ) -> None:
        names = [getattr(tc, "tool_name", "?") for tc in tool_calls]
        await self._emit(ctx, {
            "phase": "tool_execution_start",
            "tool_names": names,
            "tool_count": len(names),
        })

    async def after_tool_execution(
        self, ctx: AgentContext[Any], results: list[Any],
    ) -> None:
        tool_names = [getattr(r, "tool_name", "?") for r in results]
        errors = [getattr(r, "error", None) for r in results]
        has_error = any(e for e in errors if e)
        await self._emit(ctx, {
            "phase": "tool_execution_end",
            "tool_names": tool_names,
            "has_error": has_error,
        })

    async def after_llm_response(
        self, ctx: AgentContext[Any], response: Any,
    ) -> None:
        content_len = len(getattr(response, "content", "") or "")
        tool_count = len(getattr(response, "tool_calls", []) or [])
        await self._emit(ctx, {
            "phase": "llm_response",
            "content_length": content_len,
            "tool_calls_in_response": tool_count,
        })

    async def after_turn(
        self, ctx: AgentContext[Any], result: Any,
    ) -> None:
        stop_reason = getattr(result, "stop_reason", "") if result else ""
        await self._emit(ctx, {
            "phase": "turn_complete",
            "stop_reason": stop_reason,
        })

    async def _emit(self, ctx: AgentContext[Any], payload: dict[str, Any]) -> None:
        try:
            await self._event_bus.emit(ControlEvent(
                event_id=uuid.uuid4().hex,
                type=ControlEventType.AGENT_PROGRESS,
                scope=ControlScope(session_id=ctx.session_id),
                payload=payload,
            ))
        except Exception:
            logger.debug("ProgressReportHook emit failed", exc_info=True)
