"""Inbox Server 抽象基类。"""

from abc import ABC, abstractmethod

from .types import InboxMessage


class InboxServer(ABC):
    """Agent Inbox 的 MQ Server 抽象。

    职责：
    1. receive() - 幂等接收：同一 message_id 不会重复进入 pending 队列。
    2. consume() - 原子消费：从 pending 队列中移除并返回，保证每个消息只被交付一次。
    3. 维护 delivered_id 集合，用于在 receive() 时识别并丢弃已消费过的重复消息。
    """

    @abstractmethod
    async def receive(self, session_id: str, message: InboxMessage) -> bool:
        """接收消息。

        Returns:
            True: 消息是新的，已被保存到 pending 队列。
            False: 消息是重复的（message_id 已存在于 pending 或已交付记录中），已被忽略。
        """
        ...

    @abstractmethod
    async def consume(
        self,
        session_id: str,
        limit: int = 100,
        *,
        only_types: set[str] | None = None,
    ) -> list[InboxMessage]:
        """原子性消费消息：从 pending 队列中移除并返回，严格保证 FIFO 和 Exactly-Once 交付。

        若 ``only_types`` 非空，则仅消费 ``message_type`` 属于该集合的消息；
        不匹配的消息保持 pending（FIFO 顺序不变）。
        """
        ...

    @abstractmethod
    async def peek(self, session_id: str) -> list[InboxMessage]:
        """查看 pending 队列，不修改状态。"""
        ...

    @abstractmethod
    async def count(self, session_id: str) -> int:
        """返回 pending 消息数量。"""
        ...

    @abstractmethod
    async def clear(self, session_id: str) -> None:
        """清空指定 session 的 pending 队列和已交付记录。"""
        ...

    async def list_sessions(self) -> list[str]:
        """返回当前存在 pending 消息或已注册过的所有 session_id 列表。

        默认实现返回空列表；具体实现应覆盖此方法以支持会话发现。
        """
        return []

    async def sessions_with_pending(self) -> list[str]:
        """Session ids that currently have ≥1 pending message (count > 0).

        Default empty; concrete servers override. Distinct from
        ``list_sessions`` (which includes now-empty sessions).
        """
        return []
