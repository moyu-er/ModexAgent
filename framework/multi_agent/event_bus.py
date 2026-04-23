from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class TaskEventType(Enum):
    """任务事件类型。"""

    REGISTERED = "registered"
    STARTED = "started"
    HEARTBEAT = "heartbeat"
    PROGRESS = "progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    POLICY_TRIGGERED = "policy_triggered"
    POLICY_CHECKED = "policy_checked"
    STATUS_CHANGED = "status_changed"


@dataclass
class TaskEvent:
    """任务事件数据类。"""

    task_id: str
    event_type: TaskEventType
    timestamp: datetime = field(default_factory=datetime.now)
    conversation_id: str | None = None
    source_agent: str | None = None
    target_agent: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class TaskEventReporter(ABC):
    """任务事件报告器抽象基类。"""

    @abstractmethod
    async def report(self, event: TaskEvent) -> None:
        """报告单个事件。"""
        ...


class LoggingTaskEventReporter(TaskEventReporter):
    """日志任务事件报告器。"""

    def __init__(self, log_level: int = logging.INFO):
        self._log_level = log_level

    async def report(self, event: TaskEvent) -> None:
        logger.log(
            self._log_level,
            "[TaskEvent] %s - %s - conversation=%s payload=%s",
            event.task_id,
            event.event_type.value,
            event.conversation_id,
            event.payload,
        )


class CompositeTaskEventReporter(TaskEventReporter):
    """组合任务事件报告器。"""

    def __init__(self, reporters: list[TaskEventReporter] | None = None):
        self._reporters = list(reporters or [])

    def add(self, reporter: TaskEventReporter) -> None:
        self._reporters.append(reporter)

    async def report(self, event: TaskEvent) -> None:
        for reporter in self._reporters:
            try:
                await reporter.report(event)
            except Exception:
                logger.exception("Reporter %s failed", type(reporter).__name__)


class TaskEventBus:
    """任务事件总线。"""

    def __init__(self, reporter: TaskEventReporter | None = None):
        self._reporter = reporter or LoggingTaskEventReporter()

    async def emit(self, event: TaskEvent) -> None:
        await self._reporter.report(event)
