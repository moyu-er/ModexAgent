"""``SkillCatalog`` — the concrete deep module at the feature interface.

One catalog per native agent owns source + filter + cache + builder
coordination. It implements the consumer-owned
``commands.skill.SkillResolver`` so both command onramps resolve through the
same object the prompt section renders from.
"""

from __future__ import annotations

from modex_agent.commands.skill import ResolvedSkillCommand, SkillResolver

from .builder import DefaultSkillBuilder, SkillPromptBuilder, build_skill_command_xml
from .cache import SkillCache
from .filter import SkillFilter
from .models import ResolutionContext, Skill, SkillResource
from .source import SkillSource


class SkillCatalog(SkillResolver):
    """Facade that coordinates skill source, filtering, cache, and prompt building.

    Regular class (never frozen — it owns runtime collaborators). When *cache*
    is ``None`` skills are reloaded from *source* on every call; pass a
    :class:`DirectorySkillCache` to detect on-disk assignment additions and
    removals.
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

    async def list_skills(
        self,
        context: ResolutionContext | None = None,
    ) -> tuple[Skill, ...]:
        """Return the fully loaded, deduplicated, filtered skill list."""
        if self._cache is not None:
            return await self._cache.get_skills(
                self._source,
                self._builder,
                self._filter,
                context,
            )
        index = {s.name: s for s in await self._source.load()}
        skills = list(index.values())
        if self._filter is not None:
            skills = await self._filter.filter(skills, context)
        return tuple(skills)

    async def render_prompt(
        self,
        context: ResolutionContext | None = None,
    ) -> str:
        """Build the ``# Skills`` prompt section."""
        if self._cache is not None:
            return await self._cache.build_prompt(
                self._source,
                self._builder,
                self._filter,
                context,
            )
        skills = await self.list_skills(context)
        return await self._builder.build(skills, context)

    async def get_skill(self, name: str) -> Skill | None:
        """Get a single skill from the catalog's filtered, deduplicated view.

        A configured cache still performs its name-set freshness check, so
        on-disk additions/removals are detected. Content edits to an existing
        name are intentionally not detected because the cache has no mtime
        tracking.
        """
        return next(
            (skill for skill in await self.list_skills() if skill.name == name),
            None,
        )

    async def resolve_command(
        self,
        name: str,
        arguments: str,
    ) -> ResolvedSkillCommand | None:
        """Resolve ``/name arguments`` into the canonical XML user-content.

        ``arguments`` is the canonical text after the command token
        (surrounding whitespace stripped) — the caller guarantees it; this
        is the single rendering authority shared by both onramps.
        """
        skill = await self.get_skill(name)
        if skill is None:
            return None
        return ResolvedSkillCommand(
            skill_name=skill.name,
            xml=build_skill_command_xml(skill.name, skill.content, arguments, skill.location),
            skill_location=skill.location,
        )

    async def list_resources(self, name: str) -> tuple[SkillResource, ...]:
        """Return resources from the catalog's winning visible skill."""
        skill = await self.get_skill(name)
        return skill.resources if skill is not None else ()

    def invalidate(self) -> None:
        """Clear the cache strategy if one is configured."""
        if self._cache is not None:
            self._cache.invalidate()
