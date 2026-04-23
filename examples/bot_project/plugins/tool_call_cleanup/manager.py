"""存储层自定义实现 —— 继承 ShortTermMemoryManager 实现 Tool-Call 感知逻辑。

ToolCallAwareShortTermManager 继承默认 ShortTermMemoryManager，
在写入路径中嵌入清理逻辑，在压缩路径中处理中断残留。

与 Protocol+包装器模式相比，继承模式的优点：
- 直接复用父类的 storage/scope/lock 逻辑
- 子类只需关注差异化的清理/压缩策略
- 框架层面有 ABC 约束，类型安全
"""

from __future__ import annotations

import logging
from typing import Any

from framework.memory.core.message import ChatMessage
from framework.memory.core.scope import MemoryContext
from framework.memory.managers.short_term import ShortTermMemoryManager

from .policy import ToolCallCleanupPolicy

logger = logging.getLogger(__name__)


class ToolCallAwareShortTermManager(ShortTermMemoryManager):
    """Tool-Call 感知短期记忆管理器。

    继承 ShortTermMemoryManager，重写写入路径和压缩路径：

    1. 写入后（add_messages / replace_all_messages）立即触发清理：
       - 当前轮正常结束时，清理当前轮的 tool-call 链
       - 往前追溯，将之前所有中断轮次的 assistant-tool 链
         替换为模拟 assistant
       - 遇到之前正常结束的轮次时停止追溯

    2. 当前轮达到上限（最后一条 assistant 含 tool_calls）时：
       - 不触发清理，保留完整 tool-call 链

    3. 压缩时（_maybe_compress）：
       - 若消息流中仍有带 tool_calls 的 assistant（当前轮
         达到上限），跳过压缩
       - 否则调用父类压缩（理论上只剩 user-assistant 对，
         含模拟 assistant）
    """

    def __init__(
        self,
        *args: Any,
        policy: ToolCallCleanupPolicy | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._policy = policy or ToolCallCleanupPolicy()

    # ---- 重写写入路径 ----

    async def add_messages(
        self, context: MemoryContext, messages: list[ChatMessage | dict[str, Any]]
    ) -> None:
        """批量追加消息，写入后在锁内执行清理，再触发压缩。"""
        if not messages:
            return
        chat_msgs = self._to_chat_messages(messages)
        dict_msgs = self._to_dicts(chat_msgs)
        scope_key = self._scope.get_scope_key(context)
        async with self._storage.get_lock(scope_key).write():
            if self._config.content_transformer is not None:
                dict_msgs = await self._config.content_transformer.transform_messages(
                    dict_msgs
                )
            for message in dict_msgs:
                await self._storage.append_message(scope_key, message)
            # 写入后立即清理
            await self._do_cleanup(scope_key)
            try:
                await self._maybe_compress(context)
            except Exception:
                logger.warning(
                    "Compression failed after cleanup; messages retained.",
                    exc_info=True,
                )

    async def add_message(
        self, context: MemoryContext, message: ChatMessage | dict[str, Any]
    ) -> None:
        await self.add_messages(context, [message])

    async def replace_all_messages(
        self, context: MemoryContext, messages: list[ChatMessage | dict[str, Any]]
    ) -> None:
        """原子替换，然后触发清理。"""
        chat_msgs = self._to_chat_messages(messages)
        dict_msgs = self._to_dicts(chat_msgs)
        scope_key = self._scope.get_scope_key(context)
        async with self._storage.get_lock(scope_key).write():
            if self._config.content_transformer is not None:
                dict_msgs = await self._config.content_transformer.transform_messages(
                    dict_msgs
                )
            await self._storage.save_messages(scope_key, list(dict_msgs))
        self._last_compress_counts[scope_key] = len(dict_msgs)
        await self._do_cleanup(scope_key)

    # ---- 核心清理逻辑 ----

    async def _do_cleanup(self, scope_key: str) -> None:
        """检测并执行清理。"""
        msgs = await self._storage.load_messages(scope_key)
        cleaned = self._policy.clean(msgs)

        if len(cleaned) < len(msgs):
            await self._storage.save_messages(scope_key, cleaned)
            logger.info(
                "ToolCallAwareShortTermManager: cleaned %d \u2192 %d messages",
                len(msgs),
                len(cleaned),
            )

    # ---- 重写压缩路径 ----

    async def _maybe_compress(self, context: MemoryContext) -> None:
        """压缩控制：若仍有 tool_calls 残留（当前轮达到上限），跳过压缩。"""
        scope_key = self._scope.get_scope_key(context)
        msgs = await self._storage.load_messages(scope_key)

        # 若消息流中仍有带 tool_calls 的 assistant，说明当前轮
        # 达到上限未结束，跳过压缩以保护 tool-call 链完整性
        if any(
            m.get("role") == "assistant" and m.get("tool_calls")
            for m in msgs
        ):
            return

        # 正常场景：调用父类压缩（消息流中只剩 user-assistant 对，
        # 含模拟 assistant；压缩策略会自然处理）
        await super()._maybe_compress(context)
