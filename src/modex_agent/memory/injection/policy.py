from __future__ import annotations

from abc import ABC, abstractmethod

from modex_agent.memory.core.models import InjectionResult
from modex_agent.core.scope import MemoryContext
from modex_agent.memory.core.system import MemorySystem


class MemoryInjectionPolicy(ABC):
    """Convert MemorySystem state into a structured context bundle for LLM consumption."""

    @abstractmethod
    async def assemble(
        self,
        *,
        context: MemoryContext,
        memory_system: MemorySystem,
        query: str = "",
    ) -> InjectionResult: ...

    def injects_archive(self) -> bool:
        """True if this policy emits archive summaries into the prompt."""
        return False

    def injects_pruned(self) -> bool:
        """True if this policy emits a pruned-message catalog."""
        return False
