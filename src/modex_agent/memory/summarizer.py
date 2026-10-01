"""Memory-side summarizer collaborator contract.

The ABCs and result types the memory framework depends on when consolidating:
``ArchiveGenerator`` (pruned messages → typed archive content, called during
``cleanup_session``) and ``CoreMemoryConsolidatorBase`` (archive knowledge →
core memory files, called by the DreamEngine). Concrete summarizer agents
(the ReAct implementations) live in ``agents/summarizer/`` and implement
these contracts; result types are co-located here so both sides import them
from the memory-owned contract module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from modex_agent.core.memory_hooks import LlmUsage
from modex_agent.memory.budget import ContextBudget

if TYPE_CHECKING:
    from modex_agent.memory.archive_models import ArchiveGenerationResult
    from modex_agent.memory.prompts import PromptRegistry


class CompactionOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str
    usage: LlmUsage | None = None


class ConsolidationOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    changed: bool
    usage: LlmUsage | None = None


class SessionCompactor(ABC):
    """Contract for agents that compact a session's pruned message zone.

    The memory cleanup path calls :meth:`compact` during
    ``cleanup_session`` and :meth:`extract_topic` on the produced summary.
    Concrete implementation: :class:`~modex_agent.agents.summarizer.session_compactor.SessionCompactorAgent`.
    """

    @abstractmethod
    async def compact(
        self,
        messages: Sequence[dict[str, Any]],
        previous_summary: str | None = None,
        *,
        session_id: str = "session-compactor",
        budget: ContextBudget | None = None,
    ) -> CompactionOutcome:
        """Generate a compact summary from pruned messages."""
        ...

    @staticmethod
    @abstractmethod
    def extract_topic(summary: str, max_chars: int = 200) -> str | None:
        """Extract a short topic label from a compact summary."""
        ...


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

    Concrete implementation: :class:`~modex_agent.agents.summarizer.archive_agent.ArchiveSummarizer`.
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

    Concrete implementation: :class:`~modex_agent.agents.summarizer.consolidator.CoreMemoryConsolidator`.
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
