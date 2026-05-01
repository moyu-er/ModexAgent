"""ControlUserInterface 抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence


class ControlUserInterface(ABC):
    """控制场景的用户界面抽象。

    任意需要与用户交互的控制场景都通过此接口。
    """

    @abstractmethod
    async def render_message(
        self,
        session_id: str,
        content: str,
        metadata: Mapping[str, object] | None = None,
    ) -> str:
        """向用户展示消息（无需回复）。Returns message_id。"""
        ...

    @abstractmethod
    async def render_question(
        self,
        session_id: str,
        question: str,
        options: Sequence[str],
        timeout: float,
        metadata: Mapping[str, object] | None = None,
    ) -> str | None:
        """向用户展示问题，等待选择。超时返回 None。"""
        ...

    @abstractmethod
    async def update_message(
        self,
        session_id: str,
        message_id: str,
        content: str,
    ) -> None:
        """更新已发送的消息。"""
        ...
