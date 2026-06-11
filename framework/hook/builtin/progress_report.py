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
from framework.core.emitter import AgentResult
from framework.core.tool_manager import ToolResult
from framework.core.types import LLMResponse, ToolCall
from framework.hook.abc import (
    AfterIterationHook,
    AfterLLMResponseHook,
    AfterToolExecutionHook,
    AfterTurnHook,
    BeforeIterationHook,
    BeforeToolExecutionHook,
)

if TYPE_CHECKING:
    from framework.control.event_bus import ControlEventBus

logger = logging.getLogger(__name__)

_STOP_REASON_COMPLETED = "completed"
_STOP_REASON_MAX_ITERATIONS = "max_iterations"
_STOP_REASON_ERROR = "error"
_STOP_REASON_TURN_CANCELLED = "turn_cancelled"


def _get_agent_name(ctx: AgentContext) -> str:
    if ctx.session_meta is not None:
        return ctx.session_meta.agent_name
    if ctx.identity is not None:
        return ctx.identity.agent_id
    return "<unknown>"


def _get_iteration(ctx: AgentContext) -> int:
    state = ctx.runtime.state if ctx.runtime else None
    return getattr(state, "iteration", 0)


def _get_max_iterations(ctx: AgentContext) -> int:
    return ctx.max_iterations


class ProgressReportHook(
    BeforeIterationHook,
    AfterIterationHook,
    BeforeToolExecutionHook,
    AfterToolExecutionHook,
    AfterLLMResponseHook,
    AfterTurnHook,
):
    """推送进度事件到 ControlEventBus。

    在各 hook 点发射 AGENT_PROGRESS 事件，监控/仪表板可订阅。
    """

    @property
    def name(self) -> str:
        return "progress_report_hook"

    def __init__(self, event_bus: ControlEventBus) -> None:
        self._event_bus = event_bus

    async def before_iteration(self, ctx: AgentContext) -> None:
        await self._emit(ctx, {"phase": "iteration_start"})

    async def after_iteration(self, ctx: AgentContext) -> None:
        await self._emit(ctx, {"phase": "iteration_end"})

    async def before_tool_execution(
        self,
        ctx: AgentContext,
        tool_calls: list[ToolCall],
    ) -> None:
        for tc in tool_calls:
            payload: dict[str, Any] = {
                "phase": "tool_execution_start",
                "tool_name": tc.tool_name,
                "call_id": tc.call_id,
                "arguments": tc.arguments,
            }
            await self._emit(ctx, payload)

    async def after_tool_execution(
        self,
        ctx: AgentContext,
        results: list[ToolResult],
    ) -> None:
        for r in results:
            payload: dict[str, Any] = {
                "phase": "tool_execution_end",
                "tool_name": r.tool_name,
                "call_id": r.call_id,
                "success": r.error is None,
                "result": r.result,
            }
            await self._emit(ctx, payload)

    async def after_llm_response(
        self,
        ctx: AgentContext,
        response: LLMResponse,
    ) -> None:
        payload: dict[str, Any] = {
            "phase": "llm_response",
            "content": response.content or "",
            "finish_reason": response.finish_reason,
            "tool_names": [tc.tool_name for tc in response.tool_calls],
        }
        if response.reasoning_content is not None:
            payload["reasoning_content"] = response.reasoning_content
        if response.usage:
            payload["usage"] = response.usage
        await self._emit(ctx, payload)

    async def after_turn(
        self,
        ctx: AgentContext,
        result: AgentResult,
    ) -> None:
        stop_reason = result.stop_reason
        if stop_reason == _STOP_REASON_MAX_ITERATIONS:
            phase = "turn_max_iterations"
        elif stop_reason == _STOP_REASON_ERROR:
            phase = "turn_error"
        elif stop_reason == _STOP_REASON_TURN_CANCELLED:
            phase = "turn_cancelled"
        else:
            phase = "turn_complete"

        payload: dict[str, Any] = {"phase": phase}

        if phase == "turn_error" and result.error is not None:
            payload["error"] = result.error
        elif phase == "turn_cancelled" and result.partial_content is not None:
            payload["partial_content"] = result.partial_content

        await self._emit(ctx, payload)

    async def _emit(self, ctx: AgentContext, payload: dict[str, Any]) -> None:
        payload["agent_name"] = _get_agent_name(ctx)
        payload["session_id"] = ctx.session_id
        payload["iteration"] = _get_iteration(ctx)
        payload["max_iterations"] = _get_max_iterations(ctx)
        try:
            await self._event_bus.emit(
                ControlEvent(
                    event_id=uuid.uuid4().hex,
                    type=ControlEventType.AGENT_PROGRESS,
                    scope=ControlScope(session_id=ctx.session_id),
                    payload=payload,
                )
            )
        except Exception:
            logger.debug("ProgressReportHook emit failed", exc_info=True)
