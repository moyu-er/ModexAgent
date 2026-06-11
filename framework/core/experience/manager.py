from __future__ import annotations

from framework.core.experience.builder import ExperiencePromptBuilder
from framework.core.experience.source import FileExperienceSource

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
    ) -> str:
        """Render XML metadata block for system prompt injection.

        ``list_experiences()`` already reads each EXPERIENCE.md to extract
        frontmatter — invalid files are skipped at that level.  No need to
        re-read or re-validate here.
        """
        summaries = await self._source.list_experiences()
        summaries = summaries[:max_experiences]
        return self._builder.build(summaries)
