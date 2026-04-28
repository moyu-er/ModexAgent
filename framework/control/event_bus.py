"""ControlEventBus — 控制事件总线。

负责事件输出（agent runtime → 外部）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from framework.control.types import ControlEvent, ControlEventType

logger = logging.getLogger(__name__)

ControlEventHandler = Callable[[ControlEvent], None]


class ControlEventBus(Protocol):
    """控制事件总线协议。

    负责事件输出：agent runtime → 外部（如审批服务、监控等）。
    """

    async def emit(self, event: ControlEvent) -> None:
        """发布事件。"""
        ...

    async def subscribe(
        self,
        event_type: ControlEventType,
        handler: ControlEventHandler,
    ) -> None:
        """订阅指定类型的事件。"""
        ...


class CallbackControlEventBus:
    """基于回调的控制事件总线实现。"""

    def __init__(self) -> None:
        self._handlers: dict[ControlEventType, list[ControlEventHandler]] = {}

    async def emit(self, event: ControlEvent) -> None:
        handlers = self._handlers.get(event.type, [])
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                logger.exception(
                    "ControlEventBus handler failed for event %s",
                    event.type.value,
                )

    async def subscribe(
        self,
        event_type: ControlEventType,
        handler: ControlEventHandler,
    ) -> None:
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    def unsubscribe(
        self,
        event_type: ControlEventType,
        handler: ControlEventHandler,
    ) -> None:
        """取消订阅。"""
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)
