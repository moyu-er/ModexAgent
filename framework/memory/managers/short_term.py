"""Short-term memory manager."""
from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from framework.memory.archive import ArchiveStrategy, SemanticArchiveStrategy
from framework.memory.compression.tool_chain import (
    _find_safe_truncation_count,
    _fit_token_window,
)
from framework.memory.core.base_managers import BaseShortTermManager
from framework.memory.core.compression import (
    CompressionContext,
    CompressionResult,
    CompressionStrategy,
)
from framework.memory.core.message import ChatMessage
from framework.memory.core.scope import MemoryContext, MemoryScope
from framework.memory.core.storage import MemoryStorage
from framework.memory.utils import estimate_token_count

# Forward reference for type hint only
ContentTransformer = Any

logger = logging.getLogger(__name__)


@dataclass
class ShortTermConfig:
    """短期记忆配置。"""

    max_messages: int | None = None
    max_tokens: int | None = None
    compression_strategy: CompressionStrategy | None = None
    archive_strategy: ArchiveStrategy | None = None
    content_transformer: Any | None = None  # ContentTransformer | None
    pre_compress_callbacks: list[Any] | None = None  # list of async callables


class ShortTermMemoryManager(BaseShortTermManager):
    """管理会话内的历史消息，支持容量限制和压缩。"""

    COOLDOWN_MSG_DELTA = 5

    def __init__(
        self,
        storage: MemoryStorage,
        scope: MemoryScope,
        config: ShortTermConfig | None = None,
        history_manager: Any | None = None,
    ):
        self._storage = storage
        self._scope = scope
        self._config = config or ShortTermConfig()
        self._history_manager = history_manager
        if self._config.archive_strategy is None:
            self._config.archive_strategy = SemanticArchiveStrategy()
        self._last_compress_counts: dict[str, int] = {}

    @staticmethod
    def _to_chat_messages(
        messages: Sequence[ChatMessage | dict[str, Any]],
    ) -> list[ChatMessage]:
        """统一将 dict 转换为 ChatMessage（已是 ChatMessage 则直接返回）。"""
        return [ChatMessage.coerce(m) for m in messages]

    @staticmethod
    def _to_dicts(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
        """将 ChatMessage 列表转换为 dict 列表。"""
        return [m.to_dict() for m in messages]

    async def add_message(
        self, context: MemoryContext, message: ChatMessage | dict[str, Any]
    ) -> None:
        """追加消息到短期记忆，并在需要时触发压缩。"""
        await self.add_messages(context, [message])

    async def add_messages(
        self, context: MemoryContext, messages: Sequence[ChatMessage | dict[str, Any]]
    ) -> None:
        """批量追加消息到短期记忆，只触发一次压缩。

        适用于 ReAct 等场景：中间产生的多条消息一次性写入，
        避免在 tool-call 链尚未完成时触发压缩。

        如果配置了 content_transformer，会在存储锁内、批量写入前
        调用 transformer 转换消息内容（如将 base64 替换为占位符）。
        """
        if not messages:
            return
        chat_msgs = self._to_chat_messages(messages)
        scope_key = self._scope.get_scope_key(context)
        dict_msgs = self._to_dicts(chat_msgs)
        async with self._storage.get_lock(scope_key).write():
            # transformer 调用在锁内，确保 transform 和 write 是原子操作
            if self._config.content_transformer is not None:
                dict_msgs = await self._config.content_transformer.transform_messages(
                    dict_msgs
                )
            for msg in dict_msgs:
                await self._storage.append_message(scope_key, msg)
            try:
                await self._maybe_compress(context)
            except Exception:
                logger.warning(
                    "Compression failed after appending messages; messages retained.",
                    exc_info=True,
                )

    async def clear_messages(self, context: MemoryContext) -> None:
        """清空指定 scope 的短期消息列表。"""
        scope_key = self._scope.get_scope_key(context)
        async with self._storage.get_lock(scope_key).write():
            await self._storage.save_messages(scope_key, [])

    async def get_compression_summary(self, context: MemoryContext) -> str | None:
        """读取指定 scope 的压缩摘要，如果不存在则返回 None。"""
        scope_key = self._scope.get_scope_key(context)
        async with self._storage.get_lock(scope_key).read():
            return await self._storage.get(scope_key, ".compression_summary")

    async def get_messages(self, context: MemoryContext) -> list[ChatMessage]:
        """获取指定 scope 的完整短期消息列表。"""
        scope_key = self._scope.get_scope_key(context)
        async with self._storage.get_lock(scope_key).read():
            raw = await self._storage.load_messages(scope_key)
        return ChatMessage.from_dicts(raw)

    async def save_checkpoint(
        self, context: MemoryContext, messages: Sequence[ChatMessage | dict[str, Any]]
    ) -> None:
        scope_key = self._scope.get_scope_key(context)
        chat_msgs = self._to_chat_messages(messages)
        await self._storage.set(
            scope_key,
            ".checkpoint",
            json.dumps({"messages": self._to_dicts(chat_msgs)}, ensure_ascii=False),
        )

    async def load_checkpoint(self, context: MemoryContext) -> list[ChatMessage] | None:
        scope_key = self._scope.get_scope_key(context)
        raw = await self._storage.get(scope_key, ".checkpoint")
        dict_msgs: list[dict[str, Any]] | None = None
        if isinstance(raw, str):
            try:
                dict_msgs = json.loads(raw).get("messages", [])
            except Exception:
                return None
        elif isinstance(raw, dict):
            dict_msgs = raw.get("messages")
        if dict_msgs is None:
            return None
        return ChatMessage.from_dicts(dict_msgs)

    async def clear_checkpoint(self, context: MemoryContext) -> None:
        scope_key = self._scope.get_scope_key(context)
        await self._storage.delete(scope_key, ".checkpoint")

    async def replace_all_messages(
        self, context: MemoryContext, messages: Sequence[ChatMessage | dict[str, Any]]
    ) -> None:
        """Atomically replace all messages, triggering transformer and tracking."""
        if not messages:
            await self.clear_messages(context)
            return
        chat_msgs = self._to_chat_messages(messages)
        scope_key = self._scope.get_scope_key(context)
        dict_msgs = self._to_dicts(chat_msgs)
        async with self._storage.get_lock(scope_key).write():
            if self._config.content_transformer is not None:
                dict_msgs = await self._config.content_transformer.transform_messages(
                    dict_msgs
                )
            await self._storage.save_messages(scope_key, list(dict_msgs))
        self._last_compress_counts[scope_key] = len(dict_msgs)

    def add_pre_compress_callback(self, callback: Any) -> None:
        """Register a callback to be called before compression prunes messages."""
        if self._config.pre_compress_callbacks is None:
            self._config.pre_compress_callbacks = []
        self._config.pre_compress_callbacks.append(callback)

    async def _maybe_compress(self, context: MemoryContext) -> None:
        """检查是否超出容量限制，并执行压缩。

        压缩顺序：
        1. 若存在自定义压缩策略且存在溢出，优先使用策略进行智能压缩，
           并将生成的摘要插回短期记忆，保留上下文语义。
        2. 若压缩后仍超限，先执行按 Token 的硬截断（保证 tool-call 链完整），
           再执行按消息数的硬截断。
        3. 被移除的消息会同步归档到 HistoryArchive（如可用）。
        """
        scope_key = self._scope.get_scope_key(context)
        raw = await self._storage.load_messages(scope_key)
        messages: list[dict[str, Any]] = raw

        # dirty-bit cooldown：若自上次压缩以来新增消息数不足阈值且未超硬限制，跳过
        last_count = self._last_compress_counts.get(scope_key, 0)
        current_count = len(messages)
        over_messages = self._config.max_messages is not None and len(messages) > self._config.max_messages
        over_tokens = self._config.max_tokens is not None and estimate_token_count(messages) > self._config.max_tokens
        is_over_hard_limit = over_messages or over_tokens
        if not is_over_hard_limit and current_count - last_count < self.COOLDOWN_MSG_DELTA:
            return

        pruned: list[dict[str, Any]] = []
        summary = ""
        protected_count = 0
        min_tail_keep = 1

        # 0. Pre-compress callbacks: let providers extract insights before pruning
        if self._config.pre_compress_callbacks and is_over_hard_limit:
            for callback in self._config.pre_compress_callbacks:
                try:
                    await callback(messages, context)
                except Exception as e:
                    logger.warning("pre_compress_callback failed: %s", e)

        # 1. 自定义压缩策略：仅在消息数或 token 数仍超出预算时触发
        no_hard_limit = self._config.max_messages is None and self._config.max_tokens is None

        if self._config.compression_strategy and messages and (over_messages or over_tokens or no_hard_limit):
            assert self._config.compression_strategy is not None
            ctx = CompressionContext(
                token_count=estimate_token_count(messages),
                target_token_count=self._config.max_tokens,
            )
            result = await self._config.compression_strategy.compress(messages, ctx)
            if result.pruned_messages:
                # Convert pruned messages to dicts for storage
                pruned_dicts = [m.to_dict() if isinstance(m, ChatMessage) else m for m in result.pruned_messages]
                pruned.extend(pruned_dicts)
                if result.remaining_messages is not None:
                    # Convert remaining messages back to dicts
                    messages = [m.to_dict() if isinstance(m, ChatMessage) else m for m in result.remaining_messages]
                else:
                    messages = [m for m in messages if m not in result.pruned_messages]
            if result.summary:
                summary = result.summary
                # 将摘要保存到 KV 存储，避免以 system 消息污染历史记录
                await self._storage.set(
                    scope_key, ".compression_summary", summary
                )
            else:
                await self._storage.delete(scope_key, ".compression_summary")

        # 2. Token 限制（使用 tool-chain 感知的窗口裁剪）
        if self._config.max_tokens:
            tokens = estimate_token_count(messages)
            if tokens > self._config.max_tokens and len(messages) > max(2, protected_count + min_tail_keep):
                remaining_msgs, token_pruned = _fit_token_window(
                    messages, self._config.max_tokens, protected_count=protected_count, min_tail_keep=min_tail_keep
                )
                # Convert remaining messages and token_pruned to dicts
                messages = [m.to_dict() if isinstance(m, ChatMessage) else m for m in remaining_msgs]
                token_pruned_dicts = [m.to_dict() if isinstance(m, ChatMessage) else m for m in token_pruned]
                pruned.extend(token_pruned_dicts)

        # 3. 消息数限制（不拆分 tool 链）
        if self._config.max_messages and len(messages) > self._config.max_messages:
            excess = len(messages) - self._config.max_messages
            safe_excess = _find_safe_truncation_count(
                messages, excess, protected_count=protected_count, min_tail_keep=min_tail_keep
            )
            if safe_excess > protected_count:
                pruned.extend(messages[protected_count:safe_excess])
                messages = messages[:protected_count] + messages[safe_excess:]

        if pruned:
            await self._storage.save_messages(scope_key, messages)
            self._last_compress_counts[scope_key] = len(messages)
            logger.debug(f"Compressed {len(pruned)} messages for {scope_key}")
            if self._history_manager is not None and self._config.archive_strategy is not None:
                result = CompressionResult(
                    summary=summary,
                    pruned_messages=list(pruned),  # Convert to list[ChatMessage | dict]
                    remaining_messages=list(messages),  # Convert to list[ChatMessage | dict]
                )
                await self._config.archive_strategy.archive(
                    context,
                    list(pruned),  # Convert to list[ChatMessage | dict]
                    result,
                    self._history_manager,
                )
        else:
            self._last_compress_counts[scope_key] = current_count
