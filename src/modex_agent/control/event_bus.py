"""ControlEventBus — 控制事件总线。

负责事件输出（agent runtime → 外部）。
"""

from __future__ import annotations

import inspect
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from modex_agent.control.types import ControlEvent, ControlEventType

logger = logging.getLogger(__name__)

ControlEventHandler = Callable[[ControlEvent], Any]  # sync or async


class ControlEventBus(ABC):
    """控制事件总线协议。

    负责事件输出：agent runtime → 外部（如审批服务、监控等）。
    """

    @abstractmethod
    async def emit(self, event: ControlEvent) -> None:
        """发布事件。"""
        ...

    @abstractmethod
    async def subscribe(
        self,
        event_type: ControlEventType,
        handler: ControlEventHandler,
        session_id: str | None = None,
    ) -> None:
        """订阅指定类型的事件，可选按 session_id 过滤。"""
        ...


@dataclass
class Subscription:
    """事件订阅，支持 session 级别路由。"""

    handler: ControlEventHandler
    session_id: str | None = None  # None = 全局，接收所有 session


class CallbackControlEventBus(ControlEventBus):
    """基于回调的控制事件总线实现。支持按 session_id 路由。"""

    def __init__(self) -> None:
        self._handlers: dict[ControlEventType, list[Subscription]] = {}

    async def emit(self, event: ControlEvent) -> None:
        subs = self._handlers.get(event.type, [])
        for sub in subs:
            if sub.session_id is None or sub.session_id == event.scope.session_id:
                try:
                    result = sub.handler(event)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.exception(
                        "ControlEventBus handler failed for event %s",
                        event.type.value,
                    )

    async def subscribe(
        self,
        event_type: ControlEventType,
        handler: ControlEventHandler,
        session_id: str | None = None,
    ) -> None:
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(Subscription(handler=handler, session_id=session_id))

    def unsubscribe(
        self,
        event_type: ControlEventType,
        handler: ControlEventHandler,
        session_id: str | None = None,
    ) -> None:
        """取消订阅。*session_id* 为 None 时移除该 handler 所有订阅。"""
        subs = self._handlers.get(event_type, [])
        self._handlers[event_type] = [
            s
            for s in subs
            if not (s.handler is handler and (session_id is None or s.session_id == session_id))
        ]
