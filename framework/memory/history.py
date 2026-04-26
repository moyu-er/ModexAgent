"""MessageHistory async protocol and implementations."""

from __future__ import annotations

import asyncio
import contextlib
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

from framework.memory.core.layers import SessionMemoryManager
from framework.memory.core.message import ChatMessage
from framework.memory.core.scope import MemoryContext

if TYPE_CHECKING:
    from framework.memory.recorder import MemoryAppendRecorder


class MessageHistory(ABC):
    """Abstract async protocol for message history.

    Does NOT inherit from Sequence because synchronous iteration is
    incompatible with async storage backends.
    """

    @abstractmethod
    async def append(self, message: ChatMessage | dict[str, Any]) -> None:
        """Append a single message and persist it."""
        pass

    @abstractmethod
    async def extend(self, messages: Sequence[ChatMessage | dict[str, Any]]) -> None:
        """Append multiple messages and persist them."""
        pass

    @abstractmethod
    async def to_list(self) -> Sequence[ChatMessage]:
        """Return a copy of the current message list (ChatMessage objects)."""
        pass

    async def replace_all(
        self, messages: Sequence[ChatMessage | dict[str, Any]], *, skip_transform: bool = False
    ) -> None:
        """替换全部消息（默认实现：清空后 extend）。

        子类可优化为原子操作（如 ShortTermMessageHistory 的单锁写入）。
        参数兼容 dict，内部会自动转换为 ChatMessage。
        """
        raise NotImplementedError("replace_all is not supported by this history implementation")

    @abstractmethod
    def __len__(self) -> int:
        """Return the number of messages (best-effort for async backends)."""
        pass

    @abstractmethod
    def __iter__(self) -> Iterator[ChatMessage]:
        """Iterate over messages (best-effort for async backends)."""
        pass

    @abstractmethod
    def __getitem__(self, index: int) -> ChatMessage:
        """Get message by index (best-effort for async backends)."""
        pass


class ShortTermMessageHistory(MessageHistory):
    """Live proxy over SessionMemoryManager and MemoryContext.

    Writes directly to session memory on append/extend and reads
    from it on to_list(). Maintains a short-lived cache protected by
    asyncio.Lock using storage-first lock ordering.
    """

    def __init__(
        self,
        manager: SessionMemoryManager,
        context: MemoryContext,
        initial_messages: Sequence[ChatMessage | dict[str, Any]] | None = None,
        recorder: MemoryAppendRecorder | None = None,
    ) -> None:
        self._manager = manager
        self._context = context
        self._recorder = recorder
        self._cache: list[ChatMessage] | None = [ChatMessage.coerce(m) for m in initial_messages] if initial_messages is not None else None
        self._cache_lock = asyncio.Lock()

    async def append(self, message: ChatMessage | dict[str, Any]) -> None:
        """Append a single message via STM and invalidate cache."""
        await self._manager.add_messages(self._context, [message])
        if self._recorder is not None:
            await self._recorder.record([message], self._context)
        async with self._cache_lock:
            self._cache = None

    async def extend(self, messages: Sequence[ChatMessage | dict[str, Any]]) -> None:
        """Append multiple messages via STM (triggers compression exactly once)."""
        if not messages:
            return
        await self._manager.add_messages(self._context, list(messages))
        if self._recorder is not None:
            await self._recorder.record(list(messages), self._context)
        async with self._cache_lock:
            self._cache = None

    async def to_list(self) -> list[ChatMessage]:
        """Read from storage and cache the result."""
        # If cache is valid (not invalidated by write), return it directly.
        # This preserves message-limiting applied at construction time
        # (e.g. DefaultMemoryInjectionPolicy's max_short_term_messages).
        async with self._cache_lock:
            if self._cache is not None:
                return list(self._cache)
        stm_messages = await self._manager.get_visible_messages(self._context)
        async with self._cache_lock:
            self._cache = list(stm_messages)
        return list(stm_messages)

    async def clear(self) -> None:
        """清空短期记忆中的所有消息。"""
        await self._manager.clear(self._context)
        async with self._cache_lock:
            self._cache = None

    async def replace_all(
        self, messages: Sequence[ChatMessage | dict[str, Any]], *, skip_transform: bool = False
    ) -> None:
        """通过 Manager 接口原子替换，触发 transformer 和跟踪。"""
        _ = skip_transform
        await self._manager.replace_messages(self._context, list(messages))
        async with self._cache_lock:
            self._cache = None

    def __len__(self) -> int:
        raise RuntimeError(
            "ShortTermMessageHistory does not support synchronous len(). "
            "Use 'await history.to_list()' for async access."
        )

    def __iter__(self) -> Iterator[ChatMessage]:
        raise RuntimeError(
            "ShortTermMessageHistory does not support synchronous iteration. "
            "Use 'await history.to_list()' for async access."
        )

    def __getitem__(self, index: int) -> ChatMessage:
        raise RuntimeError(
            "ShortTermMessageHistory does not support synchronous indexing. "
            "Use 'await history.to_list()' for async access."
        )


class ListMessageHistory(MessageHistory):
    """Simple in-memory MessageHistory backed by a Python list.

    Used by InMemoryContextManager and FileContextManager.
    内部统一存储 ChatMessage；dict 输入会在入口处自动转换。
    """

    def __init__(self, messages: Sequence[ChatMessage | dict[str, Any]] | None = None) -> None:
        self._messages: list[ChatMessage] = []
        if messages:
            for m in messages:
                self._messages.append(self._coerce(m))

    @staticmethod
    def _coerce(message: ChatMessage | dict[str, Any]) -> ChatMessage:
        """将 dict 转换为 ChatMessage。"""
        return ChatMessage.coerce(message)

    async def append(self, message: ChatMessage | dict[str, Any]) -> None:
        self._messages.append(self._coerce(message))

    async def extend(self, messages: Sequence[ChatMessage | dict[str, Any]]) -> None:
        for m in messages:
            self._messages.append(self._coerce(m))

    async def to_list(self) -> list[ChatMessage]:
        """返回 ChatMessage 列表副本。"""
        return list(self._messages)

    async def replace_all(
        self, messages: Sequence[ChatMessage | dict[str, Any]], *, skip_transform: bool = False
    ) -> None:
        """替换全部消息（用于 pipeline attachments 注入等场景）。"""
        self._messages = []
        for m in messages:
            self._messages.append(self._coerce(m))

    def __len__(self) -> int:
        return len(self._messages)

    def __iter__(self) -> Iterator[ChatMessage]:
        return iter(self._messages)

    def __getitem__(self, index: int) -> ChatMessage:
        return self._messages[index]

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(messages={len(self._messages)})"


# ---------------------------------------------------------------------------
# Shared helpers (used by Pipeline and AgentSession)
# ---------------------------------------------------------------------------

async def history_to_list(
    history: MessageHistory | list[ChatMessage | dict[str, Any]] | Sequence[ChatMessage | dict[str, Any]],
) -> list[dict[str, Any]]:
    """将 MessageHistory 或消息列表转换为 list[dict]。

    Pipeline 和 AgentSession 共用此辅助函数，避免重复定义。
    返回 dict 列表以支持直接修改和序列化。
    """
    if isinstance(history, MessageHistory):
        raw = await history.to_list()
        return [m.to_dict() if isinstance(m, ChatMessage) else m for m in raw]
    result: list[dict[str, Any]] = []
    for m in history:
        if isinstance(m, ChatMessage):
            result.append(m.to_dict())
        else:
            result.append(m)
    return result


async def inject_attachments_to_history(
    history: MessageHistory | list[ChatMessage | dict[str, Any]] | Sequence[ChatMessage | dict[str, Any]],
    attachments: list[str],
) -> list[dict[str, Any]] | None:
    """将 attachments 注入到最后一条 assistant 消息的 metadata 中。

    支持 MessageHistory 实例或原始消息列表。
    ChatMessage 对象会先转换为 dict 处理，写回时重新构造。
    若传入 MessageHistory 且支持 replace_all，自动写回存储。
    """
    if not attachments:
        return None

    # 统一转为 dict 列表进行修改
    history_list = await history_to_list(history)
    for i in range(len(history_list) - 1, -1, -1):
        msg = history_list[i]
        if msg.get("role") == "assistant":
            metadata = dict(msg.get("metadata") or {})
            metadata["attachments"] = attachments
            msg["metadata"] = metadata
            break

    # 如果 history 支持原子 replace，用 ChatMessage 写回存储
    if isinstance(history, MessageHistory):
        with contextlib.suppress(NotImplementedError):
            await history.replace_all(history_list)

    return history_list


async def restore_multimodal_in_history(
    history: MessageHistory | list[ChatMessage | dict[str, Any]] | Sequence[ChatMessage | dict[str, Any]],
    multimodal_content: str | list[dict[str, Any]],
    logger: Any | None = None,
) -> list[dict[str, Any]] | None:
    """将当前用户消息的多模态内容恢复到 history（在 save->sanitize->load 后调用）。

    Pipeline 和 AgentSession 共用此辅助函数。
    内存中保存的是 sanitize 后的占位符，LLM 需要看到完整媒体内容。

    Args:
        history: MessageHistory 实例或消息列表
        multimodal_content: 完整的多模态内容（str 或 list[dict]）
        logger: 可选的 logger 实例，用于记录警告

    Returns:
        None - 恢复成功且已通过 replace_all 写回存储；
        list[dict] - 修改后的消息列表，调用方需手动赋值回 history（plain list 场景）。
    """
    try:
        hist_list = await history_to_list(history)

        if not hist_list or hist_list[-1].get("role") != "user":
            return None  # 无需恢复

        # Direct mutation on dict
        hist_list[-1]["content"] = multimodal_content

        if isinstance(history, MessageHistory):
            try:
                # skip_transform=True: 避免 content_transformer 再次将 base64 替换为占位符
                await history.replace_all(hist_list, skip_transform=True)
                return None  # 已写回
            except NotImplementedError:
                return hist_list  # 调用方需手动赋值
        else:
            return hist_list  # plain list，调用方需手动赋值
    except Exception:
        if logger:
            logger.warning("Failed to restore multimodal content in history")
        return None
