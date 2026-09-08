"""Skill value models (plan §11, §6.1) — frozen Pydantic.

``Skill``, ``SkillSummary``, ``SkillMetadata``, ``SkillResource`` are the
feature-owned cross-module values: frozen, ``extra="forbid"``, serialized
via ``model_dump()``/``model_validate()``. ``ResolutionContext`` stays a
regular runtime class — it carries a live ``ToolManager`` reference, which is
runtime state, not a value (rule 11/12).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from modex_agent.core.tool_manager import ToolManager


class SkillResource(BaseModel):
    """A resource associated with a skill (e.g., template, sample file)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    type: str
    path: str | None = None
    description: str = ""


class SkillMetadata(BaseModel):
    """Behavior-bearing skill metadata plus an opaque extension payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    disable_model_invocation: bool = False
    # Third-party frontmatter is preserved but never interpreted by the framework.
    extra: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_frontmatter(cls, data: dict[str, Any]) -> SkillMetadata:
        """Extract framework metadata from parsed SKILL.md frontmatter."""
        raw = dict(data)
        disabled = raw.pop("disable-model-invocation", None) is True
        for document_key in ("name", "description", "resources"):
            raw.pop(document_key, None)
        return cls(disable_model_invocation=disabled, extra=raw)


class Skill(BaseModel):
    """A fully loaded skill document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""
    content: str = ""
    metadata: SkillMetadata = Field(default_factory=SkillMetadata)
    source: str = ""
    location: str | None = None
    resources: tuple[SkillResource, ...] = ()


class SkillSummary(BaseModel):
    """Lightweight skill descriptor for discovery without loading full content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""
    metadata: SkillMetadata = Field(default_factory=SkillMetadata)
    source: str = ""
    location: str | None = None
    resources: tuple[SkillResource, ...] = ()

    def to_skill(self, content: str) -> Skill:
        """Hydrate a full ``Skill`` from this summary plus content."""
        return Skill(
            name=self.name,
            description=self.description,
            content=content,
            metadata=self.metadata,
            source=self.source,
            location=self.location,
            resources=self.resources,
        )


class ResolutionContext:
    """Runtime context used when resolving or filtering skills.

    Regular runtime class (not Pydantic): ``tool_manager`` is a live runtime
    object reference, not a serializable value.
    """

    def __init__(
        self,
        *,
        tool_manager: ToolManager | None = None,
        env_vars: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.tool_manager = tool_manager
        self.env_vars = dict(env_vars or {})
        self.extra = dict(extra or {})

    @classmethod
    def from_runtime(cls, tool_manager: ToolManager | None = None) -> ResolutionContext:
        """Create a context from the current process environment."""
        return cls(
            tool_manager=tool_manager,
            env_vars=dict(os.environ),
        )
