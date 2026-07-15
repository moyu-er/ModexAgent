from __future__ import annotations

from modex_agent.core.scope import MemoryContext
from modex_agent.memory.core.models import InjectionResult
from modex_agent.memory.core.system import MemorySystem
from modex_agent.memory.injection.policy import MemoryInjectionPolicy
from modex_agent.memory.pruned.manager import PrunedManager


class RestrictedInjectionPolicy(MemoryInjectionPolicy):
    """Subagent policy — session messages only, no knowledge/archive/providers.

    No message-count cap: subagent sessions compress by tokens (they carry a
    cleanup config), so token compression is the sole size governor.
    """

    def __init__(
        self,
        pruned_manager: PrunedManager | None = None,
    ) -> None:
        self._pruned_manager = pruned_manager

    def injects_pruned(self) -> bool:
        return self._pruned_manager is not None

    async def assemble(
        self,
        *,
        context: MemoryContext,
        memory_system: MemorySystem,
        query: str = "",
    ) -> InjectionResult:
        messages = await memory_system.get_history(context)
        system_prompt = ""
        if self._pruned_manager is not None:
            xml = self._pruned_manager.get_injection_xml(session_id=context.session_id or "")
            if xml:
                system_prompt = xml
        return InjectionResult(
            system_prompt=system_prompt,
            messages=list(messages),
        )
