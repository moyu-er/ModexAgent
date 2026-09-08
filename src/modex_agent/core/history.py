"""MessageHistory abstract interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from typing import Any

from modex_agent.core.message import ChatMessage


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
