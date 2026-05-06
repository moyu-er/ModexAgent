from __future__ import annotations

from framework.memory.core.models import MemoryContextBundle
from framework.memory.core.scope import MemoryContext
from framework.memory.core.system import MemorySystem
from framework.memory.injection.filter import (
    InjectionFilterStrategy,
    ToolMessageFilterStrategy,
)
from framework.memory.injection.policy import MemoryInjectionPolicy


class RestrictedInjectionPolicy(MemoryInjectionPolicy):
    """Peer/subagent policy — session messages only, no knowledge/archive/providers."""

    def __init__(
        self,
        max_session_messages: int = 50,
        filter_strategy: InjectionFilterStrategy | None = None,
    ) -> None:
        self._max_messages = max_session_messages
        self._filter = filter_strategy or ToolMessageFilterStrategy()

    async def assemble(
        self,
        *,
        context: MemoryContext,
        memory_system: MemorySystem,
        query: str = "",
    ) -> MemoryContextBundle:
        messages = await memory_system.get_history(context, max_messages=self._max_messages)
        filtered = self._filter.filter(list(messages))
        return MemoryContextBundle(
            system_sections=[],
            messages=filtered,
        )
