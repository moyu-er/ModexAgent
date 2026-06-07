from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class ExperienceSummary:
    """Lightweight metadata for injection into system prompts — no body content."""

    name: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    scenario: str = ""
    directory: str = ""


@dataclass
class Experience(ExperienceSummary):
    """Full experience with body content loaded from EXPERIENCE.md."""

    trigger: str = ""
    version: int = 1
    created_at: datetime | None = None
    pinned: bool = False
    location: Path | None = None
    body: str = ""
    frontmatter: dict[str, Any] = field(default_factory=dict)
