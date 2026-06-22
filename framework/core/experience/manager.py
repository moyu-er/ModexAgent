from __future__ import annotations

from typing import TYPE_CHECKING

from framework.core.experience.builder import ExperiencePromptBuilder
from framework.core.experience.source import FileExperienceSource

if TYPE_CHECKING:
    from framework.core.scope import MemoryContext

# Cap injected experiences to prevent system prompt bloat.
_MAX_INJECTED_EXPERIENCES = 20


class ExperienceManager:
    """Facade coordinating source + builder for experience injection.

    Usage:
        manager = ExperienceManager(source=FileExperienceSource([dir]))
        prompt = await manager.build_prompt()
        # -> XML metadata block for system prompt injection
    """

    def __init__(
        self,
        source: FileExperienceSource,
        builder: ExperiencePromptBuilder | None = None,
    ) -> None:
        self._source = source
        self._builder = builder or ExperiencePromptBuilder()

    async def build_prompt(
        self,
        max_experiences: int = _MAX_INJECTED_EXPERIENCES,
        context: "MemoryContext | None" = None,
    ) -> str:
        """Render XML metadata block for system prompt injection.

        When *context* is provided and the source has a scope configured,
        experiences resolve from the scope-specific subdirectory
        (e.g. ``{base}/{user_id}/`` for UserScope).
        """
        summaries = await self._source.list_experiences(context=context)
        summaries = summaries[:max_experiences]
        return self._builder.build(summaries)
