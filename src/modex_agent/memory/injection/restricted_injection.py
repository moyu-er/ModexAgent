from __future__ import annotations

from modex_agent.core.scope import MemoryContext
from modex_agent.memory.core.models import InjectionResult
from modex_agent.memory.core.system import MemorySystem
from modex_agent.memory.injection.policy import MemoryInjectionPolicy


class RestrictedInjectionPolicy(MemoryInjectionPolicy):
    """Subagent policy — session messages only, no core memory/archive/providers."""

    def __init__(self) -> None:
        pass

    async def assemble(
        self,
        *,
        context: MemoryContext,
        memory_system: MemorySystem,
        query: str = "",
    ) -> InjectionResult:
        messages = await memory_system.get_history(context)
        return InjectionResult(
            system_prompt="",
            messages=list(messages),
        )
