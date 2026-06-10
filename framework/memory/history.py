"""MessageHistory async protocol and implementations."""

from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from typing import Any

from framework.memory.core.message import ChatMessage


class MessageHistory(ABC):
    """Abstract async protocol for message history.

    Does NOT inherit from Sequence because synchronous iteration is
    incompatible with async storage backends.

    **Usage contract**:

    - **Primary API**: ``await history.to_list()`` — the ONLY guaranteed-safe
      way to access messages.  Returns a list you can ``len()`` and iterate.
    - ``__len__`` / ``__iter__`` / ``__getitem__`` are **NOT** guaranteed to
      work.  Implementations that use async storage backends (e.g.
      async-backed implementations) intentionally raise ``RuntimeError`` on
      these methods.  Only ``ListMessageHistory`` (in-memory) supports them.

    If you need message count, iteration, or indexed access::

        messages = await history.to_list()
        count = len(messages)
        for msg in messages:
            role = msg.role  # ChatMessage
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
        """Return a copy of the current message list (ChatMessage objects).

        This is the **primary** read API.  Always use this, never sync accessors.
        """
        pass

    async def replace_all(
        self, messages: Sequence[ChatMessage | dict[str, Any]], *, skip_transform: bool = False
    ) -> None:
        """替换全部消息（默认实现：清空后 extend）。

        子类可优化为原子操作（如异步后端的单锁写入）。
        参数兼容 dict，内部会自动转换为 ChatMessage。
        """
        raise NotImplementedError("replace_all is not supported by this history implementation")

    @abstractmethod
    def __len__(self) -> int:
        """Return message count.

        .. warning::
           Async-backed implementations (pool mode) raise ``RuntimeError``.
           Use ``len(await history.to_list())`` instead.
        """
        pass

    @abstractmethod
    def __iter__(self) -> Iterator[ChatMessage]:
        """Iterate over messages.

        .. warning::
           Async-backed implementations (pool mode) raise ``RuntimeError``.
           Use ``for msg in await history.to_list()`` instead.
        """
        pass

    @abstractmethod
    def __getitem__(self, index: int) -> ChatMessage:
        """Get message by index.

        .. warning::
           Async-backed implementations (pool mode) raise ``RuntimeError``.
           Use ``(await history.to_list())[index]`` instead.
        """
        pass


class ListMessageHistory(MessageHistory):
    """Simple in-memory MessageHistory backed by a Python list.

    Used by InMemoryContextManager.
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
# Shared helpers (used by Pipeline)
# ---------------------------------------------------------------------------

async def history_to_list(
    history: MessageHistory | list[ChatMessage | dict[str, Any]] | Sequence[ChatMessage | dict[str, Any]],
) -> list[dict[str, Any]]:
    """将 MessageHistory 或消息列表转换为 list[dict]。

    Pipeline 使用此辅助函数，避免重复定义。
    返回 dict 列表以支持直接修改和序列化。
    """
    if isinstance(history, MessageHistory):
        raw = await history.to_list()
        return [m.to_dict() for m in raw]
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

    Pipeline 使用此辅助函数。
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
