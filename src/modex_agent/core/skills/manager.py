from __future__ import annotations

from typing import TYPE_CHECKING

from .builder import DefaultSkillBuilder
from .models import ResolutionContext, Skill
from .provider import SkillProvider

if TYPE_CHECKING:
    from modex_agent.core.tool_manager import ToolManager

    from .builder import SkillPromptBuilder
    from .cache import SkillCache
    from .filter import SkillFilter
    from .source import SkillSource


class SkillManager:
    """Facade that coordinates skill source, filtering, cache, and prompt building.

    When *cache* is ``None`` skills are reloaded from *source* on every call.
    Pass a :class:`DirectorySkillCache` (or any custom :class:`SkillCache`)
    to enable change detection and partial prompt rebuild.
    """

    def __init__(
        self,
        source: SkillSource,
        skill_filter: SkillFilter | None = None,
        builder: SkillPromptBuilder | None = None,
        cache: SkillCache | None = None,
    ) -> None:
        self._source = source
        self._filter = skill_filter
        self._builder = builder or DefaultSkillBuilder()
        self._cache = cache
        self._overrides: dict[str, Skill] = {}

    async def list_skills(
        self,
        context: ResolutionContext | None = None,
    ) -> list[Skill]:
        """Return the fully loaded, deduplicated, filtered skill list."""
        if self._cache is not None:
            return await self._cache.get_skills(
                self._source,
                self._builder,
                self._filter,
                self._overrides,
                context,
            )
        index = {s.name: s for s in await self._source.load()}
        index.update(self._overrides)
        skills = list(index.values())
        if self._filter is not None:
            skills = await self._filter.filter(skills, context)
        return skills

    async def build_prompt(
        self,
        context: ResolutionContext | None = None,
    ) -> str:
        """Build the ``# Skills`` prompt section."""
        if self._cache is not None:
            return await self._cache.build_prompt(
                self._source,
                self._builder,
                self._filter,
                self._overrides,
                context,
            )
        skills = await self.list_skills(context)
        return await self._builder.build(skills, context)

    async def build_provider(
        self,
        tool_manager: ToolManager | None,
    ) -> SkillProvider | None:
        """Build a :class:`SkillProvider` for the system-prompt pipeline.

        Single convergence point: the ONLY way to turn a ``SkillManager`` into
        a pipeline provider. All ``ContextManager.load()`` implementations call
        this instead of inlining ``build_prompt`` + ``SkillProvider`` construction,
        so skill injection lives in exactly one place.

        Returns ``None`` when no skills are loaded (caller skips appending).
        """
        skill_prompt = await self.build_prompt(
            ResolutionContext.from_runtime(tool_manager=tool_manager)
        )
        if not skill_prompt:
            return None
        return SkillProvider(skill_prompt)

    async def get_skill(self, name: str) -> Skill | None:
        """Get a single skill by name, checking overrides first.

        When a cache is configured, run the same name-set freshness check
        ``build_prompt``/``get_skills`` use (``DirectorySkillCache`` scans the
        directories and clears the source cache when the skill name set
        changes).  This makes on-disk add/remove of skill files visible to
        ``get_skill`` without a restart, matching the behaviour the agent turn
        already sees via its system-prompt build.

        Content edits to an existing skill are NOT detected (the name set is
        unchanged) — consistent with the rest of the skill system, which has
        no mtime tracking.
        """
        if name in self._overrides:
            return self._overrides[name]
        if self._cache is not None:
            await self._cache.get_skills(
                self._source, self._builder, self._filter, self._overrides, None
            )
        return await self._source.load_skill(name)

    async def list_resources(self, name: str) -> list:
        """Proxy resource listing to the underlying ``SkillSource``."""
        try:
            return await self._source.list_resources(name)
        except NotImplementedError:
            return []

    def invalidate(self) -> None:
        """Clear the cache strategy if one is configured."""
        if self._cache is not None:
            self._cache.invalidate()

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
