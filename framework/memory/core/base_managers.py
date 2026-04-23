"""记忆层管理器抽象基类.

定义 MemorySystem 中四级记忆管理器的统一接口契约，
支持自定义实现替换默认实现 (如插件化的 ShortTermManager).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from framework.memory.core.message import ChatMessage
from framework.memory.core.scope import MemoryContext


class BaseShortTermManager(ABC):
    """短期记忆管理器抽象基类.

    管理会话内的历史消息，支持容量限制和压缩。
    插件可继承此类或默认实现 ShortTermMemoryManager 来定制行为。

    接口统一使用 ChatMessage，实现类在入口处自动将 dict 转换为 ChatMessage。
    """

    @abstractmethod
    async def add_message(
        self, context: MemoryContext, message: ChatMessage | dict[str, Any]
    ) -> None: ...

    @abstractmethod
    async def add_messages(
        self, context: MemoryContext, messages: Sequence[ChatMessage | dict[str, Any]]
    ) -> None: ...

    @abstractmethod
    async def clear_messages(self, context: MemoryContext) -> None: ...

    @abstractmethod
    async def get_messages(
        self, context: MemoryContext
    ) -> list[ChatMessage]: ...

    @abstractmethod
    async def replace_all_messages(
        self, context: MemoryContext, messages: Sequence[ChatMessage | dict[str, Any]]
    ) -> None: ...

    @abstractmethod
    async def get_compression_summary(
        self, context: MemoryContext
    ) -> str | None: ...

    @abstractmethod
    async def save_checkpoint(
        self, context: MemoryContext, messages: Sequence[ChatMessage | dict[str, Any]]
    ) -> None: ...

    @abstractmethod
    async def load_checkpoint(
        self, context: MemoryContext
    ) -> list[ChatMessage] | None: ...

    @abstractmethod
    async def clear_checkpoint(self, context: MemoryContext) -> None: ...

    @abstractmethod
    def add_pre_compress_callback(self, callback: Any) -> None: ...


class BaseHistoryArchiveManager(ABC):
    """历史归档管理器抽象基类."""

    @abstractmethod
    async def append(
        self,
        context: MemoryContext,
        summary: str,
        metadata: dict[str, Any],
    ) -> int: ...

    @abstractmethod
    async def get_unprocessed(
        self,
        context: MemoryContext,
        cursor_name: str = "dream",
    ) -> tuple[int, list[dict[str, Any]]]: ...

    @abstractmethod
    async def commit_cursor(
        self,
        context: MemoryContext,
        cursor_name: str,
        cursor: int,
    ) -> None: ...

    @abstractmethod
    async def get_recent(
        self,
        context: MemoryContext,
        limit: int = 5,
    ) -> list[dict[str, Any]]: ...


class BaseLongTermMemoryManager(ABC):
    """长期记忆管理器抽象基类."""

    @abstractmethod
    async def get_all(self, context: MemoryContext) -> Any: ...

    @abstractmethod
    async def update(
        self, context: MemoryContext, updates: dict[str, str]
    ) -> None: ...

    @abstractmethod
    async def apply_update(
        self, context: MemoryContext, update: Any, existing: str | None = None
    ) -> str: ...

    @abstractmethod
    async def get_file(
        self, context: MemoryContext, file_key: str
    ) -> str | None: ...

    @abstractmethod
    async def clear(self, context: MemoryContext) -> None: ...
