"""Bot configuration API payloads."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class PromptContent(BaseModel):
    """The content of an agent prompt markdown file (``agents/<name>.md``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    content: str


class PromptSummary(BaseModel):
    """List-view metadata for one ``agents/<name>.md`` file.

    Returned by ``GET /api/prompts``. ``mtime`` is an ISO 8601 string (not a
    float) so the wire shape is JSON-serializable without float precision loss.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    size_bytes: int
    mtime: str


class PromptUsage(BaseModel):
    """One agent reference to a prompt md, used by the delete-reference check.

    Returned in the ``409`` body of ``DELETE /api/prompts/{name}`` when the
    prompt is still referenced. ``agent_kind`` is a ``Literal`` (not an enum)
    because it is a wire-level discriminator consumed by the frontend, not a
    domain concept with behavior.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pool: str
    agent_kind: Literal["main", "subagent"]
    agent_name: str


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
    "PromptSummary",
    "PromptUsage",
    "SkillEntry",
    "SkillOrigin",
    "SkillSource",
]
