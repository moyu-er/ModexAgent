"""Abstract base classes and shared types for summarizer agents.

These ABCs define the contracts that the memory framework depends on,
decoupling framework code from concrete agent implementations.

Result types are co-located here so both ABC and concrete class can
import them without circular dependencies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ── Shared result types ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ArchiveSummarizerResult:
    """Result of archive generation."""

    success: bool
    archive_id: int = 0
    files_written: tuple[str, ...] = ()
    error: str | None = None


# ── Shared utilities ───────────────────────────────────────────────────────────

_prompt_registry: Any | None = None


def _get_registry() -> Any:
    """Return cached PromptRegistry, loading on first access."""
    global _prompt_registry
    if _prompt_registry is None:
        from framework.memory.prompts import create_default_registry
        _prompt_registry = create_default_registry()
    return _prompt_registry


# ── Agent ABCs ─────────────────────────────────────────────────────────────────


class ArchiveGenerator(ABC):
    """Contract for agents that generate archive files from pruned messages.

    The memory system calls ``generate()`` during ``cleanup_session()``
    to turn pruned session messages into ``context.md``, ``knowledge.md``,
    and ``index.md`` in an archive directory.

    Concrete implementation: :class:`~framework.agents.summarizer.archive_agent.ArchiveSummarizer`.
    """

    @abstractmethod
    async def generate(
        self,
        pruned_messages: Sequence[dict[str, Any]],
        archive_dir: Path,
        archive_id: int = 0,
    ) -> ArchiveSummarizerResult:
        """Generate archive files from pruned messages.

        Args:
            pruned_messages: Messages pruned from the session to summarize.
            archive_dir: Target directory for the generated files.
            archive_id: Numeric ID for this archive slot.

        Returns:
            Result indicating success/failure and which files were written.
        """
        ...


class KnowledgeConsolidatorBase(ABC):
    """Contract for agents that consolidate archive knowledge into long-term memory.

    The DreamEngine calls ``consolidate()`` to read ``knowledge.md`` files
    from one or more archives and update ``SOUL.md`` / ``USER.md`` /
    ``MEMORY.md``.

    Concrete implementation: :class:`~framework.agents.summarizer.consolidator.KnowledgeConsolidator`.
    """

    max_iterations: int
    """Default max ReAct iterations; used as base for dynamic scaling."""

    @abstractmethod
    async def consolidate(
        self,
        archive_ids: list[int],
        archive_base: Path,
        knowledge_dir: Path,
        *,
        max_iterations: int | None = None,
    ) -> bool:
        """Read knowledge.md from archives and update knowledge files.

        Args:
            archive_ids: Archive IDs to process.
            archive_base: Base directory containing archive subdirectories.
            knowledge_dir: Directory containing knowledge files to update.
            max_iterations: Optional override for max ReAct iterations.
                When ``None``, the consolidator's default is used.

        Returns:
            ``True`` if the agent ran successfully, ``False`` otherwise.
        """
        ...
