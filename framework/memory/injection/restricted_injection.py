from __future__ import annotations

from framework.memory.core.models import InjectionResult
from framework.memory.core.scope import MemoryContext
from framework.memory.core.system import MemorySystem
from framework.memory.injection.policy import MemoryInjectionPolicy


class RestrictedInjectionPolicy(MemoryInjectionPolicy):
    """Peer/subagent policy — session messages only, no knowledge/archive/providers."""

    def __init__(
        self,
        max_session_messages: int = 50,
    ) -> None:
        self._max_messages = max_session_messages

    async def assemble(
        self,
        *,
        context: MemoryContext,
        memory_system: MemorySystem,
        query: str = "",
    ) -> InjectionResult:
        messages = await memory_system.get_history(context, max_messages=self._max_messages)
        return InjectionResult(
            system_prompt="",
            messages=list(messages),
        )
