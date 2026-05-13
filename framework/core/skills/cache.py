from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .models import ResolutionContext, Skill
from .source import _parse_frontmatter

if TYPE_CHECKING:
    from .builder import SkillPromptBuilder
    from .filter import SkillFilter
    from .source import SkillSource

logger = logging.getLogger(__name__)


class SkillCache(ABC):
    """Strategy for caching and refreshing skills from sources.

    Implementations decide when cached skill data is stale and how to refresh it.
    ``SkillManager`` delegates ``list_skills()`` and ``build_prompt()`` to an
    instance of this class.
    """

    @abstractmethod
    async def get_skills(
        self,
        source: SkillSource,
        builder: SkillPromptBuilder,
        skill_filter: SkillFilter | None,
        overrides: dict[str, Skill],
        context: ResolutionContext | None,
    ) -> list[Skill]:
        """Return the latest, deduplicated skill list."""

    @abstractmethod
    async def build_prompt(
        self,
        source: SkillSource,
        builder: SkillPromptBuilder,
        skill_filter: SkillFilter | None,
        overrides: dict[str, Skill],
        context: ResolutionContext | None,
    ) -> str:
        """Return the full ``# Skills`` prompt section, rebuilding only changed portions."""

    @abstractmethod
    def invalidate(self) -> None:
        """Force-clear all cached state so the next call does a full rebuild."""


@dataclass
class _DirState:
    """Cached state for a single skill directory."""

    names: set[str] = field(default_factory=set)
    skills: list[Skill] = field(default_factory=list)
    prompt_section: str = ""


class DirectorySkillCache(SkillCache):
    """Per-directory skill cache that detects skill additions/removals via name-set comparison.

    On every ``get_skills()`` / ``build_prompt()`` call each watched directory is
    scanned (``scandir`` only — no file content reads).  When the set of skill
    names in a directory differs from the cached snapshot the source is reloaded
    and only the prompt sections for changed directories are rebuilt.

    Directory ordering matters: skills from directories earlier in the list take
    precedence when names collide (first-wins dedup).
    """

    def __init__(
        self,
        directories: list[Path],
        skill_filename: str = "SKILL.md",
        layout: str = "directory",
        exclude_names: tuple[str, ...] = ("readme.md", "index.md"),
    ) -> None:
        self._directories = [Path(d).expanduser().resolve() for d in directories]
        self._skill_filename = skill_filename
        self._layout = layout
        self._exclude_names = {n.lower() for n in exclude_names}
        self._dir_states: dict[Path, _DirState] = {}

    # -- SkillCache interface ------------------------------------------------

    async def get_skills(
        self,
        source: SkillSource,
        builder: SkillPromptBuilder,
        skill_filter: SkillFilter | None,
        overrides: dict[str, Skill],
        context: ResolutionContext | None,
    ) -> list[Skill]:
        await self._refresh_if_stale(source, builder, context)

        result: list[Skill] = []
        seen: set[str] = set()
        for directory in self._directories:
            state = self._dir_states.get(directory)
            if state is None:
                continue
            for skill in state.skills:
                if skill.name not in seen:
                    seen.add(skill.name)
                    result.append(skill)

        index = {s.name: s for s in result}
        index.update(overrides)
        result = list(index.values())

        if skill_filter is not None:
            result = await skill_filter.filter(result, context)
        return result

    async def build_prompt(
        self,
        source: SkillSource,
        builder: SkillPromptBuilder,
        skill_filter: SkillFilter | None,
        overrides: dict[str, Skill],
        context: ResolutionContext | None,
    ) -> str:
        await self._refresh_if_stale(source, builder, context)

        sections: list[str] = []
        for directory in self._directories:
            state = self._dir_states.get(directory)
            if state is not None and state.prompt_section:
                sections.append(state.prompt_section)
        return "\n\n".join(sections).strip()

    def invalidate(self) -> None:
        self._dir_states.clear()

    # -- internal ------------------------------------------------------------

    @staticmethod
    def _list_skill_names(
        directory: Path,
        layout: str = "directory",
        skill_filename: str = "SKILL.md",
        exclude_names: set[str] | None = None,
    ) -> set[str]:
        """Scan *directory* for skill names without parsing file contents."""
        resolved = Path(directory).expanduser().resolve()
        if not resolved.exists() or not resolved.is_dir():
            return set()
        names: set[str] = set()
        if layout == "directory":
            for subdir in sorted(resolved.iterdir()):
                if subdir.is_dir() and (subdir / skill_filename).exists():
                    names.add(subdir.name)
        else:
            exclude = exclude_names or set()
            for path in sorted(resolved.glob("*.md")):
                if path.name.lower() not in exclude:
                    names.add(path.stem)
        return names

    async def _refresh_if_stale(
        self,
        source: SkillSource,
        builder: SkillPromptBuilder,
        context: ResolutionContext | None,
    ) -> None:
        """Scan every directory; if any changed → full reload + partial prompt rebuild."""
        changed = False
        for directory in self._directories:
            current_names = self._list_skill_names(
                directory, self._layout, self._skill_filename, self._exclude_names,
            )
            prev = self._dir_states.get(directory)
            if prev is None or current_names != prev.names:
                changed = True
                break

        if not changed and self._dir_states:
            return

        # Clear FileSkillSource caches so list_skills() re-reads from disk
        if hasattr(source, "invalidate_cache"):
            source.invalidate_cache()

        # Load all summaries first (avoids _summary_map name-based dedup)
        summaries = await source.list_skills()
        all_skills: list[Skill] = []
        for s in summaries:
            if s.location is None:
                continue
            text = Path(s.location).read_text(encoding="utf-8")
            _, body = _parse_frontmatter(text)
            all_skills.append(s.to_skill(body))

        # Group skills by directory
        skills_by_dir: dict[Path, list[Skill]] = {d: [] for d in self._directories}
        for skill in all_skills:
            if skill.location is None:
                continue
            loc = Path(skill.location).parent.resolve()
            for directory in self._directories:
                try:
                    loc.relative_to(directory)
                    skills_by_dir[directory].append(skill)
                    break
                except ValueError:
                    continue

        for directory in self._directories:
            current_names = self._list_skill_names(
                directory, self._layout, self._skill_filename, self._exclude_names,
            )
            prev = self._dir_states.get(directory)
            if prev is None or current_names != prev.names:
                dir_skills = skills_by_dir.get(directory, [])
                prompt = ""
                if dir_skills:
                    prompt = await builder.build(dir_skills, context)
                self._dir_states[directory] = _DirState(
                    names=current_names,
                    skills=dir_skills,
                    prompt_section=prompt,
                )
