"""Inbox 消息生产端。"""

import logging
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import TYPE_CHECKING

from .server import InboxServer
from .types import InboxMessage

if TYPE_CHECKING:
    from framework.multi_agent.envelope import AgentMessageEnvelope

logger = logging.getLogger(__name__)


class BaseInboxProducer(ABC):
    """Inbox 消息生产端抽象基类。"""

    @abstractmethod
    async def send(
        self,
        session_id: str,
        envelope: "AgentMessageEnvelope",
    ) -> bool:
        """发送消息到 InboxServer。

        Returns:
            True: 消息被成功接收并持久化。
            False: 消息被判定为重复，已被忽略。
        """
        ...


class InboxProducer(BaseInboxProducer):
    """Inbox 消息生产端（本地缓存去重实现）。

    职责仅限于持久化消息到 InboxServer，不处理唤醒信号或 Broker 交互。
    唤醒/通知逻辑由上层组件（如 AgentMessageBus）负责。
    """

    def __init__(
        self,
        server: InboxServer,
        cache_size: int = 1000,
    ) -> None:
        self._server = server
        self._cache: OrderedDict[str, bool] = OrderedDict()
        self._cache_size = cache_size

    def _cache_key(self, session_id: str, message_id: str) -> str:
        return f"{session_id}:{message_id}"

    def _touch_cache(self, key: str) -> bool:
        if key in self._cache:
            self._cache.move_to_end(key)
            return True
        self._cache[key] = True
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return False

    async def send(
        self,
        session_id: str,
        envelope: "AgentMessageEnvelope",
    ) -> bool:
        metadata = dict(envelope.metadata)
        metadata["payload"] = envelope.payload
        metadata["source_kind"] = envelope.source.kind if envelope.source else "agent"
        metadata["source_name"] = envelope.source.name if envelope.source else "unknown"
        metadata["session_id"] = envelope.session_id
        if envelope.agent_session_id:
            metadata["agent_session_id"] = envelope.agent_session_id
        if envelope.invocation_id is not None:
            metadata["invocation_id"] = envelope.invocation_id
        msg = InboxMessage(
            session_id=session_id,
            source=envelope.source.name if envelope.source else "unknown",
            content=envelope.payload.get("content", ""),
            message_type=envelope.message_type,
            message_id=envelope.message_id,
            metadata=metadata,
        )
        cache_key = self._cache_key(session_id, msg.message_id)
        if self._touch_cache(cache_key):
            return False

        saved = await self._server.receive(session_id, msg)
        if not saved:
            return False

        return True


# 显式别名保留多态替换能力
LocalCacheInboxProducer = InboxProducer
