from __future__ import annotations

from typing import TYPE_CHECKING

from .builder import ProgressiveBuilder
from .models import ResolutionContext, Skill

if TYPE_CHECKING:
    from .builder import SkillPromptBuilder
    from .filter import SkillFilter
    from .source import SkillSource


class SkillManager:
    """Facade that coordinates skill source, filtering, and prompt building."""

    def __init__(
        self,
        source: SkillSource,
        skill_filter: SkillFilter | None = None,
        builder: SkillPromptBuilder | None = None,
    ) -> None:
        self._source = source
        self._filter = skill_filter
        self._builder = builder or ProgressiveBuilder()
        self._cache: dict[str, Skill] | None = None
        self._overrides: dict[str, Skill] = {}

    async def list_skills(
        self,
        context: ResolutionContext | None = None,
    ) -> list[Skill]:
        """Return the fully loaded, deduplicated, filtered skill list."""
        if self._cache is None:
            self._cache = {s.name: s for s in await self._source.load()}
        # Start from cached index (already deduplicated by source)
        index = dict(self._cache)
        # Merge overrides (overrides take precedence)
        index.update(self._overrides)
        skills = list(index.values())
        # Apply filter
        if self._filter is not None:
            skills = await self._filter.filter(skills, context)
        return skills

    async def build_prompt(
        self,
        context: ResolutionContext | None = None,
    ) -> str:
        """Build the `# Skills` prompt section."""
        skills = await self.list_skills(context)
        return await self._builder.build(skills, context)

    async def get_skill(self, name: str) -> Skill | None:
        """Get a single skill by name, checking overrides first."""
        if name in self._overrides:
            return self._overrides[name]
        return await self._source.load_skill(name)

    async def list_resources(self, name: str) -> list:
        """Proxy resource listing to the underlying ``SkillSource``.

        If the source does not implement ``list_resources()``,
        returns an empty list instead of raising.
        """
        try:
            return await self._source.list_resources(name)
        except NotImplementedError:
            return []

    def refresh(self) -> None:
        """Lazily clear the loaded skill cache."""
        self._cache = None

    def clear_overrides(self) -> None:
        """Remove all runtime skill overrides."""
        self._overrides.clear()

    async def register_skill(self, skill: Skill) -> None:
        """Register a runtime skill override."""
        self._overrides[skill.name] = skill

    async def unregister_skill(self, name: str) -> bool:
        """Remove a runtime skill override. Returns ``True`` if it existed."""
        if name in self._overrides:
            del self._overrides[name]
            return True
        return False
