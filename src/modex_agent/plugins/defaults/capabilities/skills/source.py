"""Skill sources — File / Inline / Composite adapters (plan §11.1)."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path

from modex_agent.utils.frontmatter import parse_frontmatter

from .models import Skill, SkillMetadata, SkillResource, SkillSummary

logger = logging.getLogger(__name__)


class SkillLayout(StrEnum):
    """Supported on-disk skill layouts."""

    FLAT = "flat"
    DIRECTORY = "directory"


class SkillMergeStrategy(StrEnum):
    """Duplicate handling across composed skill sources."""

    FIRST_WINS = "first_wins"
    LAST_WINS = "last_wins"
    ERROR = "error"


class SkillSource(ABC):
    """Abstract source of skills."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable source name."""

    @abstractmethod
    async def list_skills(self) -> tuple[SkillSummary, ...]:
        """Return lightweight summaries for all available skills."""

    @abstractmethod
    async def load_skill(self, name: str) -> Skill | None:
        """Return the full ``Skill`` document for ``name``, or ``None``."""

    @abstractmethod
    async def list_resources(self, name: str) -> tuple[SkillResource, ...]:
        """Return bundled resources for a named skill."""

    async def load(self) -> tuple[Skill, ...]:
        """Load all skills (default implementation)."""
        summaries = await self.list_skills()
        skills: list[Skill] = []
        for summary in summaries:
            skill = await self.load_skill(summary.name)
            if skill is not None:
                skills.append(skill)
        return tuple(skills)

    def invalidate_cache(self) -> None:
        """Clear source-local cached state when present."""


class FileSkillSource(SkillSource):
    """Load skills from directories on the filesystem."""

    def __init__(
        self,
        directories: list[Path],
        cache: bool = True,
        layout: SkillLayout | str = SkillLayout.FLAT,
        skill_filename: str = "SKILL.md",
        resource_dirs: tuple[str, ...] = ("scripts", "references", "assets"),
        exclude_names: tuple[str, ...] = ("readme.md", "index.md"),
    ) -> None:
        self._directories = [Path(d).expanduser().resolve() for d in directories]
        self._cache = cache
        self._layout = SkillLayout(layout)
        self._skill_filename = skill_filename
        self._resource_dirs = resource_dirs
        self._exclude_names = {n.lower() for n in exclude_names}
        self._listing: list[SkillSummary] | None = None
        self._summary_map: dict[str, SkillSummary] = {}
        self._contents: dict[str, str] = {}

    @property
    def directories(self) -> list[Path]:
        return list(self._directories)

    @property
    def layout(self) -> SkillLayout:
        return self._layout

    @property
    def name(self) -> str:
        return f"file:{':'.join(str(d) for d in self._directories)}"

    def list_skill_names(self, directory: Path) -> set[str]:
        """Return the set of skill names in *directory* without parsing file contents.

        ``"directory"`` layout: returns subdirectory names that contain ``SKILL.md``.
        ``"flat"`` layout: returns ``.md`` file stems, excluding ``exclude_names``.
        Returns an empty set if the directory does not exist.
        """
        resolved = directory.expanduser().resolve()
        if not resolved.exists() or not resolved.is_dir():
            return set()
        names: set[str] = set()
        if self._layout is SkillLayout.DIRECTORY:
            for subdir in sorted(resolved.iterdir()):
                if subdir.is_dir() and (subdir / self._skill_filename).exists():
                    names.add(subdir.name)
        else:
            for path in sorted(resolved.glob("*.md")):
                if path.name.lower() not in self._exclude_names:
                    names.add(path.stem)
        return names

    def invalidate_cache(self) -> None:
        """Clear internal caches so the next ``list_skills`` / ``load_skill`` re-reads from disk."""
        self._listing = None
        self._summary_map.clear()
        self._contents.clear()

    def _iter_skill_paths(self, directory: Path) -> Iterator[Path]:
        """Yield candidate skill markdown paths based on layout."""
        if self._layout is SkillLayout.DIRECTORY:
            for subdir in sorted(directory.iterdir()):
                if subdir.is_dir():
                    candidate = subdir / self._skill_filename
                    if candidate.exists():
                        yield candidate
        else:
            for path in sorted(directory.rglob("*.md")):
                if path.name.lower() not in self._exclude_names:
                    yield path

    async def list_skills(self) -> tuple[SkillSummary, ...]:
        if self._cache and self._listing is not None:
            return tuple(self._listing)
        summaries: list[SkillSummary] = []
        for directory in self._directories:
            if not directory.exists():
                continue
            for path in self._iter_skill_paths(directory):
                try:
                    text = path.read_text(encoding="utf-8")
                    frontmatter, _ = parse_frontmatter(text)
                    # Default name: directory name in directory layout, filename stem in flat layout
                    default_name = (
                        path.parent.name
                        if self._layout is SkillLayout.DIRECTORY
                        else path.stem
                    )
                    raw_name = frontmatter.get("name", default_name)
                    meta = SkillMetadata.from_dict(frontmatter)

                    # Auto-scan bundled resources
                    skill_dir = path.parent
                    auto_resources: list[SkillResource] = []
                    for rtype in self._resource_dirs:
                        rdir = skill_dir / rtype
                        if rdir.exists() and rdir.is_dir():
                            auto_resources.append(
                                SkillResource(name=rtype, type=rtype, path=str(rdir))
                            )
                    frontmatter_resources = [
                        SkillResource.model_validate(r) for r in frontmatter.get("resources", [])
                    ]
                    resources_by_name = {r.name: r for r in auto_resources}
                    for r in frontmatter_resources:
                        resources_by_name[r.name] = r
                    resources = tuple(resources_by_name.values())

                    summary = SkillSummary(
                        name=str(raw_name),
                        description=frontmatter.get("description", ""),
                        metadata=meta,
                        source=self.name,
                        location=str(path),
                        resources=resources,
                    )
                    summaries.append(summary)
                    if self._cache:
                        self._contents[summary.name] = text
                except Exception as exc:  # pragma: no cover
                    logger.warning("Skipping malformed skill file %s: %s", path, exc)
        if self._cache:
            self._listing = list(summaries)
            self._summary_map = {s.name: s for s in summaries}
        return tuple(summaries)

    async def list_resources(self, name: str) -> tuple[SkillResource, ...]:
        """Return bundled resources for a named skill from the cached summary."""
        if self._cache:
            summary = self._summary_map.get(name)
            if summary is None:
                await self.list_skills()
                summary = self._summary_map.get(name)
            if summary is not None:
                return summary.resources
        else:
            summaries = await self.list_skills()
            summary_map = {s.name: s for s in summaries}
            summary = summary_map.get(name)
            if summary is not None:
                return summary.resources
        return ()

    async def load_skill(self, name: str) -> Skill | None:
        summary = None
        if self._cache:
            summary = self._summary_map.get(name)
            if summary is None:
                await self.list_skills()
                summary = self._summary_map.get(name)

        if self._cache and name in self._contents:
            text = self._contents[name]
        else:
            if not self._cache or summary is None:
                summaries = await self.list_skills()
                summary_map = {s.name: s for s in summaries}
                summary = summary_map.get(name)
            if summary is None or summary.location is None:
                return None
            text = Path(summary.location).read_text(encoding="utf-8")
            if self._cache:
                self._contents[name] = text

        frontmatter, content = parse_frontmatter(text)
        if summary is not None:
            return summary.to_skill(content)

        # Fallback if summary somehow missing (shouldn't happen)
        meta = SkillMetadata.from_dict(frontmatter)
        return Skill(
            name=name,
            description=frontmatter.get("description", ""),
            content=content,
            metadata=meta,
            source=self.name,
            location=None,
            resources=tuple(
                SkillResource.model_validate(r) for r in frontmatter.get("resources", [])
            ),
        )


class InlineSkillSource(SkillSource):
    """In-memory skill source backed by a list of ``Skill`` objects."""

    def __init__(self, skills: list[Skill], name: str = "inline") -> None:
        self._skills = {s.name: s for s in skills}
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def list_skills(self) -> tuple[SkillSummary, ...]:
        return tuple(
            SkillSummary(
                name=s.name,
                description=s.description,
                metadata=s.metadata,
                source=self.name,
                location=s.location,
                resources=s.resources,
            )
            for s in self._skills.values()
        )

    async def load_skill(self, name: str) -> Skill | None:
        return self._skills.get(name)

    async def list_resources(self, name: str) -> tuple[SkillResource, ...]:
        skill = self._skills.get(name)
        return skill.resources if skill is not None else ()


class CompositeSkillSource(SkillSource):
    """Combine multiple sources with configurable deduplication."""

    def __init__(
        self,
        sources: list[SkillSource],
        merge_strategy: SkillMergeStrategy | str = SkillMergeStrategy.LAST_WINS,
    ) -> None:
        self._sources = list(sources)
        self._merge_strategy = SkillMergeStrategy(merge_strategy)

    @property
    def name(self) -> str:
        return f"composite:{':'.join(s.name for s in self._sources)}"

    async def list_skills(self) -> tuple[SkillSummary, ...]:
        index: dict[str, SkillSummary] = {}
        for source in self._sources:
            try:
                for summary in await source.list_skills():
                    if self._merge_strategy is SkillMergeStrategy.LAST_WINS:
                        index[summary.name] = summary
                    elif self._merge_strategy is SkillMergeStrategy.ERROR:
                        if summary.name in index:
                            raise ValueError(f"Duplicate skill '{summary.name}' across sources")
                        index[summary.name] = summary
                    else:
                        index.setdefault(summary.name, summary)
            except ValueError:
                raise
            except Exception as exc:  # pragma: no cover
                logger.warning("Source %s failed during list_skills: %s", source.name, exc)
        return tuple(index.values())

    async def load_skill(self, name: str) -> Skill | None:
        if self._merge_strategy is SkillMergeStrategy.LAST_WINS:
            for source in reversed(self._sources):
                skill = await source.load_skill(name)
                if skill is not None:
                    return skill
        elif self._merge_strategy is SkillMergeStrategy.ERROR:
            found: Skill | None = None
            for source in self._sources:
                skill = await source.load_skill(name)
                if skill is not None:
                    if found is not None:
                        raise ValueError(f"Duplicate skill '{name}' across sources")
                    found = skill
            return found
        else:
            for source in self._sources:
                skill = await source.load_skill(name)
                if skill is not None:
                    return skill
        return None

    async def list_resources(self, name: str) -> tuple[SkillResource, ...]:
        sources: Iterator[SkillSource]
        if self._merge_strategy is SkillMergeStrategy.LAST_WINS:
            sources = reversed(self._sources)
        else:
            sources = iter(self._sources)
        found: tuple[SkillResource, ...] | None = None
        for source in sources:
            skill = await source.load_skill(name)
            if skill is None:
                continue
            resources = await source.list_resources(name)
            if self._merge_strategy is not SkillMergeStrategy.ERROR:
                return resources
            if found is not None:
                raise ValueError(f"Duplicate skill '{name}' across sources")
            found = resources
        return found or ()

    def invalidate_cache(self) -> None:
        for source in self._sources:
            source.invalidate_cache()
