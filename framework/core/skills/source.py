from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .models import Skill, SkillMetadata, SkillResource, SkillSummary

logger = logging.getLogger(__name__)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter between --- fences."""
    lines = text.splitlines(keepends=True)
    if not lines or not lines[0].strip().startswith("---"):
        return {}, text
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end == -1:
        return {}, text
    try:
        import yaml
        frontmatter = yaml.safe_load("".join(lines[1:end])) or {}
    except Exception as exc:  # pragma: no cover
        logger.warning("Failed to parse frontmatter: %s", exc)
        frontmatter = {}
    content = "".join(lines[end + 1 :])
    return frontmatter, content


class SkillSource(ABC):
    """Abstract source of skills."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable source name."""

    @abstractmethod
    async def list_skills(self) -> list[SkillSummary]:
        """Return lightweight summaries for all available skills."""

    @abstractmethod
    async def load_skill(self, name: str) -> Skill | None:
        """Return the full ``Skill`` document for ``name``, or ``None``."""

    async def list_resources(self, name: str) -> list[SkillResource]:
        """Return bundled resources for a named skill.

        Default implementation raises ``NotImplementedError``.
        Subclasses that support resource discovery should override this.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement list_resources()"
        )

    async def load(self) -> list[Skill]:
        """Load all skills（default implementation）."""
        summaries = await self.list_skills()
        skills: list[Skill] = []
        for summary in summaries:
            skill = await self.load_skill(summary.name)
            if skill is not None:
                skills.append(skill)
        return skills


class FileSkillSource(SkillSource):
    """Load skills from directories on the filesystem."""

    def __init__(
        self,
        directories: list[Path],
        cache: bool = True,
        layout: str = "flat",
        skill_filename: str = "SKILL.md",
        resource_dirs: tuple[str, ...] = ("scripts", "references", "assets"),
        exclude_names: tuple[str, ...] = ("readme.md", "index.md"),
    ) -> None:
        self._directories = [Path(d).expanduser().resolve() for d in directories]
        self._cache = cache
        self._layout = layout
        self._skill_filename = skill_filename
        self._resource_dirs = resource_dirs
        self._exclude_names = {n.lower() for n in exclude_names}
        self._listing: list[SkillSummary] | None = None
        self._summary_map: dict[str, SkillSummary] = {}
        self._contents: dict[str, str] = {}

    @property
    def name(self) -> str:
        return f"file:{':'.join(str(d) for d in self._directories)}"

    def _iter_skill_paths(self, directory: Path):
        """Yield candidate skill markdown paths based on layout."""
        if self._layout == "directory":
            for subdir in sorted(directory.iterdir()):
                if subdir.is_dir():
                    candidate = subdir / self._skill_filename
                    if candidate.exists():
                        yield candidate
        else:
            for path in sorted(directory.rglob("*.md")):
                if path.name.lower() not in self._exclude_names:
                    yield path

    async def list_skills(self) -> list[SkillSummary]:
        if self._cache and self._listing is not None:
            return list(self._listing)
        summaries: list[SkillSummary] = []
        for directory in self._directories:
            if not directory.exists():
                continue
            for path in self._iter_skill_paths(directory):
                try:
                    text = path.read_text(encoding="utf-8")
                    frontmatter, _ = _parse_frontmatter(text)
                    # Default name: directory name in directory layout, filename stem in flat layout
                    if self._layout == "directory":
                        default_name = path.parent.name
                    else:
                        default_name = path.stem
                    raw_name = frontmatter.get("name", default_name)
                    meta = SkillMetadata.from_dict(frontmatter)

                    # Auto-scan bundled resources
                    skill_dir = path.parent
                    auto_resources: list[SkillResource] = []
                    for rtype in self._resource_dirs:
                        rdir = skill_dir / rtype
                        if rdir.exists() and rdir.is_dir():
                            auto_resources.append(SkillResource(name=rtype, type=rtype, path=str(rdir)))
                    frontmatter_resources = [
                        SkillResource(**r) for r in frontmatter.get("resources", [])
                    ]
                    resources_by_name = {r.name: r for r in auto_resources}
                    for r in frontmatter_resources:
                        resources_by_name[r.name] = r
                    resources = list(resources_by_name.values())

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
        return summaries

    async def list_resources(self, name: str) -> list[SkillResource]:
        """Return bundled resources for a named skill from the cached summary."""
        if self._cache:
            summary = self._summary_map.get(name)
            if summary is None:
                await self.list_skills()
                summary = self._summary_map.get(name)
            if summary is not None:
                return list(summary.resources)
        else:
            summaries = await self.list_skills()
            summary_map = {s.name: s for s in summaries}
            summary = summary_map.get(name)
            if summary is not None:
                return list(summary.resources)
        return []

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

        frontmatter, content = _parse_frontmatter(text)
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
            resources=[SkillResource(**r) for r in frontmatter.get("resources", [])],
        )


class InlineSkillSource(SkillSource):
    """In-memory skill source backed by a list of ``Skill`` objects."""

    def __init__(self, skills: list[Skill], name: str = "inline") -> None:
        self._skills = {s.name: s for s in skills}
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def list_skills(self) -> list[SkillSummary]:
        return [
            SkillSummary(
                name=s.name,
                description=s.description,
                metadata=s.metadata,
                source=self.name,
                location=s.location,
                resources=list(s.resources),
            )
            for s in self._skills.values()
        ]

    async def load_skill(self, name: str) -> Skill | None:
        return self._skills.get(name)


class CompositeSkillSource(SkillSource):
    """Combine multiple sources with configurable deduplication."""

    def __init__(
        self,
        sources: list[SkillSource],
        merge_strategy: str = "last_wins",
    ) -> None:
        self._sources = list(sources)
        self._merge_strategy = merge_strategy

    @property
    def name(self) -> str:
        return f"composite:{':'.join(s.name for s in self._sources)}"

    async def list_skills(self) -> list[SkillSummary]:
        index: dict[str, SkillSummary] = {}
        for source in self._sources:
            try:
                for summary in await source.list_skills():
                    if self._merge_strategy == "last_wins":
                        index[summary.name] = summary
                    elif self._merge_strategy == "error":
                        if summary.name in index:
                            raise ValueError(
                                f"Duplicate skill '{summary.name}' across sources"
                            )
                        index[summary.name] = summary
                    else:
                        index.setdefault(summary.name, summary)
            except ValueError:
                raise
            except Exception as exc:  # pragma: no cover
                logger.warning("Source %s failed during list_skills: %s", source.name, exc)
        return list(index.values())

    async def load_skill(self, name: str) -> Skill | None:
        if self._merge_strategy == "last_wins":
            for source in reversed(self._sources):
                skill = await source.load_skill(name)
                if skill is not None:
                    return skill
        elif self._merge_strategy == "error":
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
