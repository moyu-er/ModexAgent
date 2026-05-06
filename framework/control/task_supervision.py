"""Task supervision — runtime monitoring and policy enforcement for async tasks.

Absorbed from the former multi_agent/intervention.py module.
Provides timeout monitoring, heartbeat emission, and policy-based
task cancellation. This is part of the control/ runtime plane.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from framework.multi_agent.coordinator import TaskCoordinator, TaskRecord

T = TypeVar("T")
logger = logging.getLogger(__name__)


class SupervisionAction(Enum):
    """监督动作枚举。"""

    PASS = "pass"
    CANCEL = "cancel"
    PAUSE = "pause"
    NOTIFY = "notify"
    THROTTLE = "throttle"
    REDIRECT = "redirect"


@dataclass
class SupervisionResult:
    """监督结果。"""

    action: SupervisionAction = SupervisionAction.PASS
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class TaskSupervisionPolicy(ABC):
    """任务监督策略抽象基类。"""

    policy_type: str = ""

    @abstractmethod
    async def check(self, task_record: TaskRecord) -> SupervisionResult:
        """检查策略条件。"""
        ...

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> TaskSupervisionPolicy:
        """从配置创建策略实例（子类应覆盖）。"""
        return cls(**config)


class NoOpSupervisionPolicy(TaskSupervisionPolicy):
    """显式关闭所有监督的占位策略。"""

    policy_type = "no_op"

    async def check(self, task_record: TaskRecord) -> SupervisionResult:
        return SupervisionResult(action=SupervisionAction.PASS, reason="No-op policy")


class TimeoutSupervisionPolicy(TaskSupervisionPolicy):
    """超时取消策略。"""

    policy_type = "timeout_cancellation"

    def __init__(self, deadline: float):
        self.deadline = deadline

    async def check(self, task_record: TaskRecord) -> SupervisionResult:
        if time.time() > self.deadline:
            return SupervisionResult(
                action=SupervisionAction.CANCEL,
                reason=f"Task {task_record.task_id} exceeded deadline {datetime.fromtimestamp(self.deadline).isoformat()}",
            )
        return SupervisionResult(action=SupervisionAction.PASS, reason="Within deadline")

    @classmethod
    def from_duration(cls, seconds: float = 180.0) -> TimeoutSupervisionPolicy:
        return cls(deadline=time.time() + seconds)


class TaskSupervisor:
    """任务监督器，不侵入 Agent 层。"""

    def __init__(
        self,
        coordinator: TaskCoordinator,
        check_interval: float = 5.0,
        emit_heartbeat: bool = True,
    ):
        self._coordinator = coordinator
        self._check_interval = check_interval
        self._emit_heartbeat = emit_heartbeat

    async def supervise(self, task_id: str, coro: Coroutine[Any, Any, T]) -> T:
        """包裹任意协程，在开始前和运行中持续执行策略检查。"""
        from framework.multi_agent.event_bus import TaskEvent, TaskEventType

        try:
            record = await self._coordinator.get_task_record(task_id)
        except Exception:
            logger.warning("TaskCoordinator unavailable during pre-check, proceeding without policy check")
            record = None

        if record:
            for result in await record.check_all():
                if result.action == SupervisionAction.CANCEL:
                    raise asyncio.CancelledError(result.reason)

        try:
            await self._coordinator.update_task_status(task_id, "running")
        except Exception:
            logger.warning("Failed to update task status to running, proceeding")

        main_task = asyncio.create_task(coro)
        monitor_task = asyncio.create_task(self._monitor(task_id, main_task))

        bus = self._coordinator.event_bus
        if bus:
            try:
                await bus.emit(TaskEvent(task_id=task_id, event_type=TaskEventType.STARTED))
            except Exception:
                logger.exception("EventBus emit STARTED failed")

        try:
            result = await main_task
            if bus:
                try:
                    await bus.emit(
                        TaskEvent(
                            task_id=task_id,
                            event_type=TaskEventType.COMPLETED,
                            payload={"stop_reason": getattr(result, "stop_reason", None)},
                        )
                    )
                except Exception:
                    logger.exception("EventBus emit COMPLETED failed")
            return result
        except asyncio.CancelledError:
            if bus:
                try:
                    await bus.emit(TaskEvent(task_id=task_id, event_type=TaskEventType.CANCELLED))
                except Exception:
                    logger.exception("EventBus emit CANCELLED failed")
            with contextlib.suppress(Exception):
                await self._coordinator.update_task_status(
                    task_id, "cancelled", {"reason": "policy_or_external_cancel"}
                )
            raise
        except Exception as exc:
            if bus:
                try:
                    await bus.emit(
                        TaskEvent(
                            task_id=task_id,
                            event_type=TaskEventType.FAILED,
                            payload={"error": str(exc), "error_type": type(exc).__name__},
                        )
                    )
                except Exception:
                    logger.exception("EventBus emit FAILED failed")
            raise
        finally:
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task

    async def _monitor(self, task_id: str, main_task: asyncio.Task) -> None:
        from framework.multi_agent.event_bus import TaskEvent, TaskEventType

        change_event = self._coordinator.on_policy_change(task_id)
        bus = self._coordinator.event_bus
        while not main_task.done():
            try:
                await asyncio.wait_for(change_event.wait(), timeout=self._check_interval)
                change_event = self._coordinator.on_policy_change(task_id)
            except TimeoutError:
                pass
            except Exception:
                logger.exception("Error waiting for policy change event")

            try:
                record = await self._coordinator.get_task_record(task_id)
            except Exception:
                logger.warning("TaskCoordinator unavailable during monitor, skipping check")
                continue

            if not record:
                continue

            triggered_results: list[SupervisionResult] = []
            for result in await record.check_all():
                if result.action == SupervisionAction.CANCEL and not main_task.done():
                    main_task.cancel()
                    return
                if result.action == SupervisionAction.NOTIFY:
                    triggered_results.append(result)
                elif result.action not in (SupervisionAction.PASS, SupervisionAction.CANCEL):
                    logger.debug("Unhandled supervision action: %s", result.action)

            if bus:
                if triggered_results:
                    try:
                        await bus.emit(
                            TaskEvent(
                                task_id=task_id,
                                event_type=TaskEventType.POLICY_TRIGGERED,
                                payload={
                                    "results": [
                                        {"action": r.action.value, "reason": r.reason, "metadata": r.metadata}
                                        for r in triggered_results
                                    ]
                                },
                            )
                        )
                    except Exception:
                        logger.exception("EventBus emit POLICY_TRIGGERED failed")
                if self._emit_heartbeat:
                    try:
                        await bus.emit(
                            TaskEvent(
                                task_id=task_id,
                                event_type=TaskEventType.HEARTBEAT,
                                payload={
                                    "status": record.status,
                                    "elapsed_seconds": time.time() - (record.started_at or record.created_at),
                                },
                            )
                        )
                    except Exception:
                        logger.exception("EventBus emit HEARTBEAT failed")
