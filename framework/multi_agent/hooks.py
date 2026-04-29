"""Multi-agent 专属 Hook 实现。

PeerAutoSendHook、SubagentMemoryCleanupHook 已迁入 framework.hook.builtin。
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
    """任务进度 Hook：向 TaskEventBus 报告进度。"""

    def __init__(self, task_id: str, event_bus: TaskEventBus):
        self._task_id = task_id
        self._event_bus = event_bus
        self._iteration = 0
        self._tool_calls = 0

    async def before_iteration(self, ctx: AgentContext) -> None:
        self._iteration += 1

    async def before_tool_execution(self, ctx: AgentContext, tool_calls: list[Any]) -> None:
        self._tool_calls += len(tool_calls)
        if self._event_bus:
            from .event_bus import TaskEvent, TaskEventType

            try:
                await self._event_bus.emit(
                    TaskEvent(
                        task_id=self._task_id,
                        event_type=TaskEventType.PROGRESS,
                        payload={
                            "iteration": self._iteration,
                            "tool_calls": self._tool_calls,
                            "progress_percent": min(95, self._iteration * 10),
                        },
                    )
                )
            except Exception:
                logger.exception("TaskProgressHook emit failed")
