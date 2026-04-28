"""Multi-agent 专属 Hook 实现。

PeerAutoSendHook、SubagentMemoryCleanupHook 已迁入 framework.hook.builtin。
本文件仅保留 multi-agent 任务级 Hook（TaskProgressHook、TaskInterventionHook）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from framework.core.agent import AgentContext

if TYPE_CHECKING:
    from .coordinator import TaskCoordinator
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


class TaskInterventionHook:
    """任务干预 Hook：在每次 ReAct 迭代前检查该 turn 是否被策略要求取消。"""

    def __init__(self, task_id: str, coordinator: TaskCoordinator):
        self._task_id = task_id
        self._coordinator = coordinator

    async def before_iteration(self, ctx: AgentContext) -> None:
        record = await self._coordinator.get_task_record(self._task_id)
        if not record:
            return
        results = await record.check_all()
        for result in results:
            if result.action.value == "cancel":
                raise asyncio.CancelledError(result.reason)
