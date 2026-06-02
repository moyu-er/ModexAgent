"""Multi-agent 专属 Hook 实现。

SubagentAutoSendHook 已迁入 framework.hook.builtin。
SubagentMemoryCleanupHook 已移除（零生产实例化）。
TaskInterventionHook 已被 ControlDrainInterceptor + ControlCommand 机制替代，已移除。
本文件仅保留 TaskProgressHook。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from framework.core.agent import AgentContext

if TYPE_CHECKING:
    from .event_bus import TaskEventBus

logger = logging.getLogger(__name__)


class TaskProgressHook:
    """任务进度 Hook：向 TaskEventBus 报告进度。

    按 session_id 隔离计数器，防止 pool 模式下多 session 竞态。
    """

    def __init__(self, task_id: str, event_bus: TaskEventBus) -> None:
        self._task_id = task_id
        self._event_bus = event_bus
        # session_id → {"iteration": int, "tool_calls": int}
        self._state: dict[str, dict[str, int]] = {}

    def _get_state(self, ctx: AgentContext) -> dict[str, int]:
        sid = ctx.session_id or "default"
        if sid not in self._state:
            self._state[sid] = {"iteration": 0, "tool_calls": 0}
        return self._state[sid]

    async def before_iteration(self, ctx: AgentContext) -> None:
        self._get_state(ctx)["iteration"] += 1

    async def before_tool_execution(self, ctx: AgentContext, tool_calls: list[Any]) -> None:
        state = self._get_state(ctx)
        state["tool_calls"] += len(tool_calls)
        if self._event_bus:
            from .event_bus import TaskEvent, TaskEventType

            try:
                await self._event_bus.emit(
                    TaskEvent(
                        task_id=self._task_id,
                        event_type=TaskEventType.PROGRESS,
                        payload={
                            "iteration": state["iteration"],
                            "tool_calls": state["tool_calls"],
                            "progress_percent": min(95, state["iteration"] * 10),
                        },
                    )
                )
            except Exception:
                logger.exception("TaskProgressHook emit failed")
