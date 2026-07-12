"""Bot configuration API payloads."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class PromptContent(BaseModel):
    """The content of an agent prompt markdown file (``agents/<name>.md``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    content: str


class SkillSource(StrEnum):
    """Skill source exposed to the WebUI."""

    GLOBAL = "global"
    LOCAL = "local"


class SkillOrigin(StrEnum):
    """Where a global skill originated (repo library vs user home)."""

    REPO = "repo"
    USER = "user"


class SkillEntry(BaseModel):
    """A skill name, source, origin, and short description parsed from SKILL.md."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    source: SkillSource = SkillSource.GLOBAL
    origin: SkillOrigin | None = None
    description: str = ""


__all__ = [
    "PromptContent",
    "SkillEntry",
    "SkillOrigin",
    "SkillSource",
]
