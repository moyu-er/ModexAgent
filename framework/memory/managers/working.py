"""Working memory manager."""

from typing import Any

from framework.memory.core.scope import MemoryContext, MemoryScope
from framework.memory.core.message import ChatMessage

from framework.memory.core.base_managers import BaseWorkingMemoryManager


class WorkingMemoryManager(BaseWorkingMemoryManager):
    """管理当前活跃对话上下文（单次推理过程中的增量消息）。

    职责：
    1. 缓存当前 turn 新增的消息
    2. 在 turn 结束时提供 clear/flush 接口

    注意：WorkingMemoryManager 本身不持久化，数据在进程重启后丢失。
    """

    def __init__(self, scope: MemoryScope):
        self._scope = scope
        self._cache: dict[str, list[ChatMessage]] = {}

    @staticmethod
    def _to_chat_message(message: ChatMessage | dict[str, Any]) -> ChatMessage:
        """统一将 dict 转换为 ChatMessage（已是 ChatMessage 则直接返回）。"""
        return ChatMessage.coerce(message)

    def add_message(self, context: MemoryContext, message: ChatMessage | dict[str, Any]) -> None:
        """向当前 turn 的缓存中添加一条消息。"""
        scope_key = self._scope.get_scope_key(context)
        self._cache.setdefault(scope_key, []).append(self._to_chat_message(message))

    def get_messages(self, context: MemoryContext) -> list[ChatMessage]:
        """获取指定 scope 的当前缓存消息（副本）。"""
        scope_key = self._scope.get_scope_key(context)
        return list(self._cache.get(scope_key, []))

    def clear(self, context: MemoryContext) -> list[ChatMessage]:
        """清空缓存并返回之前缓存的消息列表。"""
        scope_key = self._scope.get_scope_key(context)
        return self._cache.pop(scope_key, [])
