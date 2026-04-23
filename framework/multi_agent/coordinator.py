from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .event_bus import TaskEvent, TaskEventBus, TaskEventType

if TYPE_CHECKING:
    from .intervention import TaskInterventionPolicy

logger = logging.getLogger(__name__)


@dataclass
class TaskRecord:
    """任务记录。"""

    task_id: str
    task_type: str
    created_at: float
    started_at: float | None = None
    updated_at: float | None = None
    status: str = "pending"
    conversation_id: str | None = None
    source_agent: str | None = None
    target_agent: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    policies: list[TaskInterventionPolicy] = field(default_factory=list)
    events: list[TaskEvent] = field(default_factory=list)
    max_events: int = 1000
    expires_at: float | None = None

    def bind_policy(self, policy: TaskInterventionPolicy) -> None:
        """绑定策略，同类型替换。"""
        existing = next(
            (i for i, p in enumerate(self.policies) if p.policy_type and p.policy_type == policy.policy_type),
            None,
        )
        if existing is not None:
            self.policies[existing] = policy
        else:
            self.policies.append(policy)
        self.updated_at = time.time()

    def replace_policies(self, policies: list[TaskInterventionPolicy]) -> None:
        """原子替换策略列表。"""
        self.policies = policies
        self.updated_at = time.time()

    async def check_all(self) -> list[Any]:
        """执行所有策略检查。"""
        return [await p.check(self) for p in self.policies]

    def append_event(self, event: TaskEvent) -> None:
        """追加事件并限制容量。"""
        self.events.append(event)
        if len(self.events) > self.max_events:
            self.events = self.events[-self.max_events:]
        self.updated_at = time.time()


class TaskCoordinator(ABC):
    """中央任务协调器抽象基类。"""

    @abstractmethod
    async def register_task(self, task_id: str, record: TaskRecord) -> None:
        ...

    @abstractmethod
    async def bind_policy(self, task_id: str, policy: TaskInterventionPolicy) -> None:
        ...

    @abstractmethod
    async def replace_policies(self, task_id: str, policies: list[TaskInterventionPolicy]) -> None:
        ...

    @abstractmethod
    async def get_task_record(self, task_id: str) -> TaskRecord | None:
        ...

    @abstractmethod
    async def get_task_records_by_conversation(self, conversation_id: str) -> list[TaskRecord]:
        ...

    @abstractmethod
    async def get_task_records_by_status(self, status: str) -> list[TaskRecord]:
        ...

    @property
    @abstractmethod
    def event_bus(self) -> TaskEventBus | None:
        ...

    @abstractmethod
    async def update_task_status(
        self, task_id: str, status: str, metadata: dict[str, Any] | None = None
    ) -> None:
        ...

    @abstractmethod
    async def revoke_task(self, task_id: str) -> None:
        ...

    def on_policy_change(self, task_id: str) -> asyncio.Event:
        return asyncio.Event()


class NullTaskCoordinator(TaskCoordinator):
    """空对象模式的 TaskCoordinator。"""

    @property
    def event_bus(self) -> TaskEventBus | None:
        return None

    async def register_task(self, task_id: str, record: TaskRecord) -> None:
        pass

    async def bind_policy(self, task_id: str, policy: TaskInterventionPolicy) -> None:
        pass

    async def replace_policies(self, task_id: str, policies: list[TaskInterventionPolicy]) -> None:
        pass

    async def get_task_record(self, task_id: str) -> TaskRecord | None:
        return None

    async def get_task_records_by_conversation(self, conversation_id: str) -> list[TaskRecord]:
        return []

    async def get_task_records_by_status(self, status: str) -> list[TaskRecord]:
        return []

    async def update_task_status(
        self, task_id: str, status: str, metadata: dict[str, Any] | None = None
    ) -> None:
        pass

    async def revoke_task(self, task_id: str) -> None:
        pass


class InMemoryTaskCoordinator(TaskCoordinator):
    """内存级任务协调器。"""

    _DEFAULT_TTL_SECONDS: float = 3600.0

    def __init__(
        self,
        event_bus: TaskEventBus | None = None,
        default_ttl_seconds: float | None = None,
    ):
        self._tasks: dict[str, TaskRecord] = {}
        self._change_events: dict[str, asyncio.Event] = {}
        self._event_bus = event_bus
        self._ttl = default_ttl_seconds if default_ttl_seconds is not None else self._DEFAULT_TTL_SECONDS

    @property
    def event_bus(self) -> TaskEventBus | None:
        return self._event_bus

    def _prune_expired(self) -> None:
        now = time.time()
        expired = [
            tid
            for tid, rec in self._tasks.items()
            if (rec.status in ("completed", "cancelled", "failed") and (now - (rec.updated_at or rec.created_at)) > self._ttl)
            or (rec.expires_at is not None and now > rec.expires_at)
        ]
        for tid in expired:
            self._tasks.pop(tid, None)
            self._change_events.pop(tid, None)

    async def register_task(self, task_id: str, record: TaskRecord) -> None:
        self._tasks[task_id] = record
        if self._event_bus:
            try:
                await self._event_bus.emit(
                    TaskEvent(
                        task_id=task_id,
                        event_type=TaskEventType.REGISTERED,
                        conversation_id=record.conversation_id,
                        source_agent=record.source_agent,
                        target_agent=record.target_agent,
                        payload={"task_type": record.task_type, "created_at": record.created_at},
                    )
                )
            except Exception:
                logger.exception("EventBus emit failed during register_task for %s", task_id)

    async def bind_policy(self, task_id: str, policy: TaskInterventionPolicy) -> None:
        rec = self._tasks.get(task_id)
        if rec is None:
            return
        rec.bind_policy(policy)
        event = self._change_events.get(task_id)
        if event and not event.is_set():
            event.set()
        if self._event_bus:
            try:
                await self._event_bus.emit(
                    TaskEvent(
                        task_id=task_id,
                        event_type=TaskEventType.POLICY_CHECKED,
                        payload={"policy_type": policy.policy_type},
                    )
                )
            except Exception:
                logger.exception("EventBus emit failed during bind_policy for %s", task_id)

    async def replace_policies(self, task_id: str, policies: list[TaskInterventionPolicy]) -> None:
        rec = self._tasks.get(task_id)
        if rec is None:
            return
        rec.replace_policies(policies)
        event = self._change_events.get(task_id)
        if event and not event.is_set():
            event.set()

    async def get_task_record(self, task_id: str) -> TaskRecord | None:
        self._prune_expired()
        return self._tasks.get(task_id)

    async def get_task_records_by_conversation(self, conversation_id: str) -> list[TaskRecord]:
        self._prune_expired()
        return [r for r in self._tasks.values() if r.conversation_id == conversation_id]

    async def get_task_records_by_status(self, status: str) -> list[TaskRecord]:
        self._prune_expired()
        return [r for r in self._tasks.values() if r.status == status]

    async def update_task_status(
        self, task_id: str, status: str, metadata: dict[str, Any] | None = None
    ) -> None:
        rec = self._tasks.get(task_id)
        if rec is None:
            return
        old_status = rec.status
        rec.status = status
        rec.updated_at = time.time()
        if metadata:
            rec.metadata.update(metadata)
        if self._event_bus:
            try:
                await self._event_bus.emit(
                    TaskEvent(
                        task_id=task_id,
                        event_type=TaskEventType.STATUS_CHANGED,
                        conversation_id=rec.conversation_id,
                        payload={"old_status": old_status, "new_status": status, **(metadata or {})},
                    )
                )
            except Exception:
                logger.exception("EventBus emit failed during update_task_status for %s", task_id)

    async def revoke_task(self, task_id: str) -> None:
        self._tasks.pop(task_id, None)
        self._change_events.pop(task_id, None)

    def on_policy_change(self, task_id: str) -> asyncio.Event:
        event = self._change_events.setdefault(task_id, asyncio.Event())
        if event.is_set():
            event.clear()
        return event
