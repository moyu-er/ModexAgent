from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from pathvalidate import sanitize_filename as _path_sanitize

from modex_agent.core.experience.models import Experience, ExperienceSummary
from modex_agent.core.frontmatter import parse_frontmatter
from modex_agent.core.scope import scope_path_key

if TYPE_CHECKING:
    from modex_agent.core.scope import MemoryContext, Scope

logger = logging.getLogger(__name__)

_EXPERIENCE_FILENAME = "EXPERIENCE.md"
_COLLAPSED_DASHES = re.compile(r"-{2,}")


def sanitize_name(name: str) -> str:
    """Normalize experience name to a safe directory name."""
    name = name.lower().strip().replace(" ", "-")
    safe = _path_sanitize(name, replacement_text="-")
    safe = _COLLAPSED_DASHES.sub("-", safe)
    return safe.strip("-") or "untitled"


def coerce_tags(raw: object) -> list[str]:
    """Coerce raw frontmatter ``tags`` to ``list[str]``.

    YAML parses unquoted numerics (e.g. ``tags: [12306]``) as ``int`` and
    explicit nulls as ``None``.  The ``ExperienceSummary.tags`` contract is
    ``list[str]``; without coercion, ``",".join(exp.tags)`` in the prompt
    builder raises ``TypeError`` and silently drops *all* experiences from
    the system prompt.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(tag) for tag in raw if tag is not None]


class FileExperienceSource:
    """Load experiences from filesystem directories.

    Each experience is a subdirectory containing EXPERIENCE.md with
    YAML frontmatter and markdown body.

    Supports optional ``scope`` for per-user (UserScope) or global
    (GlobalScope) experience isolation.  When scope is set, the
    effective directory becomes ``{base_dir}/{scope_key}/``.
    """

    def __init__(
        self,
        directories: list[Path],
        scope: Scope | None = None,
    ) -> None:
        self._directories = [d.expanduser().resolve() for d in directories]
        self._scope = scope

    @property
    def directories(self) -> list[Path]:
        return list(self._directories)

    @property
    def scope(self) -> Scope | None:
        return self._scope

    def _resolve_dirs(self, context: MemoryContext | None = None) -> list[Path]:
        """Return directories, appending scope_key subdir when scope is set."""
        if self._scope is None or context is None:
            return self._directories
        scope_key = scope_path_key(self._scope, context)
        if not scope_key:
            return self._directories
        return [d / scope_key for d in self._directories]

    async def list_experiences(
        self, context: "MemoryContext | None" = None
    ) -> list[ExperienceSummary]:
        """Scan all directories for EXPERIENCE.md files, return metadata only."""
        summaries: list[ExperienceSummary] = []
        seen: set[str] = set()
        for directory in self._resolve_dirs(context):
            if not directory.exists():
                continue
            for exp_dir in sorted(directory.iterdir()):
                if not exp_dir.is_dir():
                    continue
                md_path = exp_dir / _EXPERIENCE_FILENAME
                if not md_path.exists():
                    continue
                try:
                    text = md_path.read_text(encoding="utf-8")
                    frontmatter, _ = parse_frontmatter(text)
                    name = exp_dir.name
                    if name in seen:
                        continue
                    seen.add(name)

                    summaries.append(
                        ExperienceSummary(
                            name=name,
                            description=str(frontmatter.get("description", "")),
                            tags=coerce_tags(frontmatter.get("tags", [])),
                            scenario=str(frontmatter.get("scenario", "")),
                            directory=str(exp_dir.resolve()),
                        )
                    )
                except Exception:
                    logger.debug("Skipping malformed experience: %s", md_path, exc_info=True)
        return summaries

    async def load_experience(
        self, name: str, context: "MemoryContext | None" = None
    ) -> Experience | None:
        """Load full EXPERIENCE.md content by directory *name*.

        Matches by directory name — the canonical identity for experiences.
        """
        for directory in self._resolve_dirs(context):
            if not directory.exists():
                continue
            for exp_dir in sorted(directory.iterdir()):
                if not exp_dir.is_dir():
                    continue
                if exp_dir.name != name:
                    continue
                md_path = exp_dir / _EXPERIENCE_FILENAME
                if not md_path.exists():
                    continue

                try:
                    text = md_path.read_text(encoding="utf-8")
                    frontmatter, body = parse_frontmatter(text)
                except Exception:
                    continue

                try:
                    return Experience(
                        name=name,
                        description=str(frontmatter.get("description", "")),
                        tags=coerce_tags(frontmatter.get("tags", [])),
                        scenario=str(frontmatter.get("scenario", "")),
                        trigger=str(frontmatter.get("trigger", "")),
                        version=int(frontmatter.get("version", 1)),
                        pinned=bool(frontmatter.get("pinned", False)),
                        location=md_path.resolve(),
                        body=body.strip(),
                        frontmatter=frontmatter,
                    )
                except Exception:
                    logger.debug("Failed to load experience %s: %s", name, md_path, exc_info=True)
        return None
