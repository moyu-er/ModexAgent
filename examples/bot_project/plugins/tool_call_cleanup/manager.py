"""Tool-Call 感知 Session 管理器 -- 组合模式包装 SessionMemoryManager.

ToolCallAwareSessionManager 实现 SessionMemoryManager ABC，
内部委托给原始 manager（装饰器模式），在写入后自动清理 tool-call 中间步骤。

与旧版继承具体实现类不同，新版使用组合：
- 不依赖任何 concrete 类的私有字段
- 通过 SessionMemoryManager ABC 交互，可适配任何实现
- 清理逻辑委托给 ToolCallCleanupPolicy（纯策略）
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from modex_agent.core.message import ChatMessage
from modex_agent.core.scope import MemoryContext
from modex_agent.memory.core.layers import SessionMemoryManager
from modex_agent.memory.core.models import StorageRevision

from .policy import ToolCallCleanupPolicy

logger = logging.getLogger(__name__)


class ToolCallAwareSessionManager(SessionMemoryManager):
    """Tool-Call 感知 Session 管理器（装饰器模式）。

    包装原始 SessionMemoryManager，在每次 add_messages 后执行清理：
    1. 当 ReAct 轮正常完成（最后一条 assistant 无 tool_calls）时，
       清理所有 tool-call 中间步骤
    2. 当 ReAct 轮被中断（最后一条 assistant 含 tool_calls）时，
       不触发清理，保留完整 tool-call 链
    3. 后续轮次完成后，向前追溯清理历史中断轮次
    """

    def __init__(
        self,
        inner: SessionMemoryManager,
        policy: ToolCallCleanupPolicy | None = None,
    ) -> None:
        self._inner = inner
        self._policy = policy or ToolCallCleanupPolicy()

    async def add_messages(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, Any]],
    ) -> StorageRevision:
        revision = await self._inner.add_messages(context, messages)
        await self._cleanup(context)
        return revision

    async def get_recent_messages(
        self,
        context: MemoryContext,
        limit: int | None = None,
    ) -> list[ChatMessage]:
        return await self._inner.get_recent_messages(context, limit=limit)

    async def get_all_messages(self, context: MemoryContext) -> list[ChatMessage]:
        return await self._inner.get_all_messages(context)

    async def get_all_messages_raw(self, context: MemoryContext) -> list[ChatMessage]:
        return await self._inner.get_all_messages_raw(context)

    async def retain_messages(
        self,
        context: MemoryContext,
        keep_messages: Sequence[ChatMessage | dict[str, Any]],
        expected_revision: StorageRevision,
    ) -> StorageRevision | None:
        revision = await self._inner.retain_messages(
            context,
            keep_messages,
            expected_revision,
        )
        if revision is not None:
            await self._cleanup(context)
        return revision

    async def clear(self, context: MemoryContext) -> None:
        await self._inner.clear(context)

    async def replace_messages(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, Any]],
    ) -> StorageRevision:
        revision = await self._inner.replace_messages(context, messages)
        await self._cleanup(context)
        return revision

    async def replace_messages_if_revision(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, Any]],
        expected_revision: StorageRevision,
        state_updates: Mapping[str, Any] | None = None,
    ) -> StorageRevision | None:
        revision = await self._inner.replace_messages_if_revision(
            context,
            messages,
            expected_revision,
            state_updates,
        )
        if revision is not None:
            await self._cleanup(context)
        return revision

    async def get_revision(self, context: MemoryContext) -> StorageRevision:
        return await self._inner.get_revision(context)

    async def _cleanup(self, context: MemoryContext) -> None:
        all_msgs = await self._inner.get_all_messages(context)
        dict_msgs = [m.to_dict() for m in all_msgs]
        cleaned = self._policy.clean(dict_msgs)

        if len(cleaned) < len(dict_msgs):
            await self._inner.replace_messages(context, cleaned)
            logger.info(
                "ToolCallAwareSessionManager: cleaned %d -> %d messages",
                len(dict_msgs),
                len(cleaned),
            )
