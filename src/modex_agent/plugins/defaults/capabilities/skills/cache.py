"""DirectorySkillCache — per-directory change detection + partial rebuilds."""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.utils.frontmatter import parse_frontmatter

from .models import ResolutionContext, Skill
from .source import SkillLayout

if TYPE_CHECKING:
    from .builder import SkillPromptBuilder
    from .filter import SkillFilter
    from .source import SkillSource

logger = logging.getLogger(__name__)


class SkillCache(ABC):
    """Strategy for caching and refreshing skills from sources.

    Implementations decide when cached skill data is stale and how to refresh it.
    ``SkillCatalog`` delegates ``list_skills()`` and ``build_prompt()`` to an
    instance of this class.
    """

    @abstractmethod
    async def get_skills(
        self,
        source: SkillSource,
        builder: SkillPromptBuilder,
        skill_filter: SkillFilter | None,
        context: ResolutionContext | None,
    ) -> tuple[Skill, ...]:
        """Return the latest, deduplicated skill list."""

    @abstractmethod
    async def build_prompt(
        self,
        source: SkillSource,
        builder: SkillPromptBuilder,
        skill_filter: SkillFilter | None,
        context: ResolutionContext | None,
    ) -> str:
        """Return the full ``# Skills`` prompt section, rebuilding only changed portions."""

    @abstractmethod
    def invalidate(self) -> None:
        """Force-clear all cached state so the next call does a full rebuild."""


class _DirState:
    """Cached state for a single skill directory."""

    def __init__(
        self,
        *,
        snapshot: dict[str, tuple[str, str]] | None = None,
        skills: list[Skill] | None = None,
    ) -> None:
        self.snapshot = dict(snapshot or {})
        self.skills = list(skills or ())


class DirectorySkillCache(SkillCache):
    """Per-directory cache keyed by each assigned ``SKILL.md`` snapshot.

    On every ``get_skills()`` / ``build_prompt()`` call each watched directory is
    fingerprinted by resolved target and content digest. Additions, removals,
    content edits, and assignment-link target changes reload the source.

    Directory ordering matters: skills from directories later in the list take
    precedence when names collide (last-wins dedup).
    """

    def __init__(
        self,
        directories: list[Path],
        skill_filename: str = "SKILL.md",
        layout: SkillLayout | str = SkillLayout.DIRECTORY,
        exclude_names: tuple[str, ...] = ("readme.md", "index.md"),
    ) -> None:
        self._directories = [Path(d).expanduser().resolve() for d in directories]
        self._skill_filename = skill_filename
        self._layout = SkillLayout(layout)
        self._exclude_names = {n.lower() for n in exclude_names}
        self._dir_states: dict[Path, _DirState] = {}

    # -- SkillCache interface ------------------------------------------------

    async def get_skills(
        self,
        source: SkillSource,
        builder: SkillPromptBuilder,
        skill_filter: SkillFilter | None,
        context: ResolutionContext | None,
    ) -> tuple[Skill, ...]:
        await self._refresh_if_stale(source)

        index: dict[str, Skill] = {}
        for directory in self._directories:
            state = self._dir_states.get(directory)
            if state is None:
                continue
            for skill in state.skills:
                index[skill.name] = skill

        result = list(index.values())
        if skill_filter is not None:
            result = await skill_filter.filter(result, context)
        return tuple(result)

    async def build_prompt(
        self,
        source: SkillSource,
        builder: SkillPromptBuilder,
        skill_filter: SkillFilter | None,
        context: ResolutionContext | None,
    ) -> str:
        skills = await self.get_skills(source, builder, skill_filter, context)
        return await builder.build(skills, context)

    def invalidate(self) -> None:
        self._dir_states.clear()

    # -- internal ------------------------------------------------------------

    @staticmethod
    def _skill_snapshot(
        directory: Path,
        layout: SkillLayout = SkillLayout.DIRECTORY,
        skill_filename: str = "SKILL.md",
        exclude_names: set[str] | None = None,
    ) -> dict[str, tuple[str, str]]:
        """Return ``name -> (resolved path, content digest)`` for a directory."""
        resolved = Path(directory).expanduser().resolve()
        if not resolved.exists() or not resolved.is_dir():
            return {}
        snapshot: dict[str, tuple[str, str]] = {}
        if layout is SkillLayout.DIRECTORY:
            for subdir in sorted(resolved.iterdir()):
                candidate = subdir / skill_filename
                if subdir.is_dir() and candidate.exists():
                    snapshot[subdir.name] = (
                        str(candidate.resolve()),
                        hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    )
        else:
            exclude = exclude_names or set()
            for path in sorted(resolved.glob("*.md")):
                if path.name.lower() not in exclude:
                    snapshot[path.stem] = (
                        str(path.resolve()),
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                    )
        return snapshot

    async def _refresh_if_stale(
        self,
        source: SkillSource,
    ) -> None:
        """Reload source state when any watched directory snapshot changes."""
        changed = False
        for directory in self._directories:
            current_snapshot = self._skill_snapshot(
                directory,
                self._layout,
                self._skill_filename,
                self._exclude_names,
            )
            prev = self._dir_states.get(directory)
            if prev is None or current_snapshot != prev.snapshot:
                changed = True
                break

        if not changed and self._dir_states:
            return

        source.invalidate_cache()

        # Load all summaries first (avoids _summary_map name-based dedup)
        summaries = await source.list_skills()
        all_skills: list[Skill] = []
        for s in summaries:
            if s.location is None:
                continue
            text = Path(s.location).read_text(encoding="utf-8")
            _, body = parse_frontmatter(text)
            all_skills.append(s.to_skill(body))

        # Group skills by directory
        skills_by_dir: dict[Path, list[Skill]] = {d: [] for d in self._directories}
        for skill in all_skills:
            if skill.location is None:
                continue
            loc = Path(skill.location).parent
            for directory in self._directories:
                try:
                    loc.relative_to(directory)
                    skills_by_dir[directory].append(skill)
                    break
                except ValueError:
                    continue

        for directory in self._directories:
            current_snapshot = self._skill_snapshot(
                directory,
                self._layout,
                self._skill_filename,
                self._exclude_names,
            )
            prev = self._dir_states.get(directory)
            if prev is None or current_snapshot != prev.snapshot:
                dir_skills = skills_by_dir.get(directory, [])
                self._dir_states[directory] = _DirState(
                    snapshot=current_snapshot,
                    skills=dir_skills,
                )
