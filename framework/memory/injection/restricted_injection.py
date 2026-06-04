from __future__ import annotations

from framework.memory.core.models import InjectionResult
from framework.memory.core.scope import MemoryContext
from framework.memory.core.system import MemorySystem
from framework.memory.injection.policy import MemoryInjectionPolicy
from framework.memory.pruned.manager import PrunedManager


class RestrictedInjectionPolicy(MemoryInjectionPolicy):
    """Peer/subagent policy — session messages only, no knowledge/archive/providers."""

    def __init__(
        self,
        max_session_messages: int = 50,
        pruned_manager: PrunedManager | None = None,
    ) -> None:
        self._max_messages = max_session_messages
        self._pruned_manager = pruned_manager

    async def assemble(
        self,
        *,
        context: MemoryContext,
        memory_system: MemorySystem,
        query: str = "",
    ) -> InjectionResult:
        messages = await memory_system.get_history(context, max_messages=self._max_messages)
        system_prompt = ""
        if self._pruned_manager is not None:
            xml = self._pruned_manager.get_injection_xml(session_id=context.session_id or "")
            if xml:
                system_prompt = xml
        return InjectionResult(
            system_prompt=system_prompt,
            messages=list(messages),
        )
