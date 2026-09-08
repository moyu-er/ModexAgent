"""MemoryProvider ABC — the memory package's append-fanout contract.

Owned by the memory package (relocated from ``plugins/abc.py`` when the
memory-provider plugin slot was removed — plan ``slot-rationalization``
§1.L1/§2.C4). :class:`MemoryAppendRecorder` fans out appended messages to
every registered provider. Plugins (the higher tier) may implement this
ABC — memory never imports from plugins.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.core.message import ChatMessage
    from modex_agent.memory.scope import MemoryContext


class MemoryProvider(ABC):
    """Append-fanout memory capability (see module docstring)."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def add(
        self,
        messages: list[ChatMessage],
        context: MemoryContext,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def search(
        self,
        query: str,
        context: MemoryContext,
        limit: int = 5,
    ) -> list[dict[str, Any]]: ...

    async def prefetch(self, query: str, context: MemoryContext) -> str | None:
        return None
