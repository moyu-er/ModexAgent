"""Abstract base classes and shared types for summarizer agents.

These ABCs define the contracts that the memory framework depends on,
decoupling framework code from concrete agent implementations.

Result types are co-located here so both ABC and concrete class can
import them without circular dependencies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.agents.summarizer.outcomes import ConsolidationOutcome

if TYPE_CHECKING:
    from modex_agent.memory.archive_models import ArchiveGenerationResult
    from modex_agent.memory.prompts import PromptRegistry

# ── Shared utilities ───────────────────────────────────────────────────────────

_prompt_registry: PromptRegistry | None = None


def _get_registry() -> PromptRegistry:
    """Return cached PromptRegistry, loading on first access."""
    global _prompt_registry
    if _prompt_registry is None:
        from modex_agent.memory.prompts import create_default_registry

        _prompt_registry = create_default_registry()
    return _prompt_registry


# ── Agent ABCs ─────────────────────────────────────────────────────────────────


class ArchiveGenerator(ABC):
    """Contract for agents that generate typed archive content from pruned messages.

    The memory system calls ``generate()`` during ``cleanup_session()``
    to turn pruned session messages into backend-neutral archive content.

    Concrete implementation: :class:`~framework.agents.summarizer.archive_agent.ArchiveSummarizer`.
    """

    @abstractmethod
    async def generate(
        self,
        pruned_messages: Sequence[dict[str, Any]],
    ) -> ArchiveGenerationResult:
        """Generate archive content from pruned messages.

        Args:
            pruned_messages: Messages pruned from the session to summarize.
        Returns:
            Typed context, knowledge, and index content ready for persistence.
        """
        ...


class CoreMemoryConsolidatorBase(ABC):
    """Contract for agents that consolidate archive knowledge into long-term memory.

    The DreamEngine calls ``consolidate()`` to read ``knowledge.md`` files
    from one or more archives and update ``SOUL.md`` / ``USER.md`` /
    ``MEMORY.md``.

    Concrete implementation: :class:`~framework.agents.summarizer.consolidator.CoreMemoryConsolidator`.
    """

    max_iterations: int
    """Default max ReAct iterations; used as base for dynamic scaling."""

    @abstractmethod
    async def consolidate(
        self,
        archive_ids: list[int],
        archive_base: Path,
        core_memory_dir: Path,
        *,
        max_iterations: int | None = None,
        invocation_id: str = "",
    ) -> ConsolidationOutcome:
        """Read knowledge.md from archives and update core memory files.

        Args:
            archive_ids: Archive IDs to process.
            archive_base: Base directory containing archive subdirectories.
            core_memory_dir: Directory containing core memory files to update.
            max_iterations: Optional override for max ReAct iterations.
                When ``None``, the consolidator's default is used.
            invocation_id: Caller-supplied UUID for trace correlation.

        Returns:
            Consolidation status and operation-local LLM usage.
        """
        ...
