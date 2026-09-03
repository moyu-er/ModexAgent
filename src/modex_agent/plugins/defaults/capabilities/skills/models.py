"""Skill value models (plan §11, §6.1) — frozen Pydantic.

``Skill``, ``SkillSummary``, ``SkillMetadata``, ``SkillResource`` are the
feature-owned cross-module values: frozen, ``extra="forbid"``, serialized
via ``model_dump()``/``model_validate()``. ``ResolutionContext`` stays a
regular runtime class — it carries a live ``ToolManager`` reference, which is
runtime state, not a value (rule 11/12).
"""

from __future__ import annotations

import json
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
    """Structured metadata for a skill."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requires_tools: tuple[str, ...] = ()
    requires_bins: tuple[str, ...] = ()
    requires_env: tuple[str, ...] = ()
    always: bool = False
    tags: tuple[str, ...] = ()
    author: str = ""
    version: str = ""
    # Unknown third-party frontmatter is an intentionally open extension payload.
    extra: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillMetadata:
        """Parse metadata from a dict with dual-format compatibility.

        Supports flat YAML frontmatter, nested ``requires: {tools: []}``,
        and nanobot JSON-in-YAML ``metadata:`` blocks.
        Unknown keys are collected into ``extra``.
        """
        if not isinstance(data, dict):
            data = {}
        raw = dict(data)

        # Step 1: handle metadata JSON-in-YAML
        metadata_raw = raw.pop("metadata", None)
        nanobot_prefix = None
        if isinstance(metadata_raw, str):
            try:
                metadata_raw = json.loads(metadata_raw)
            except (json.JSONDecodeError, TypeError):
                metadata_raw = {}
        if isinstance(metadata_raw, dict):
            payload = metadata_raw.get("nanobot")
            if payload is not None:
                nanobot_prefix = "nanobot"
            else:
                payload = metadata_raw.get("openclaw")
                if payload is not None:
                    nanobot_prefix = "openclaw"
                else:
                    payload = metadata_raw
            if isinstance(payload, dict):
                merged = dict(payload)
                merged.update(raw)
                raw = merged

        # Step 2: expand nested requires
        requires = raw.pop("requires", None)
        if isinstance(requires, dict):
            if "tools" in requires and "requires_tools" not in raw:
                raw["requires_tools"] = requires["tools"]
            if "bins" in requires and "requires_bins" not in raw:
                raw["requires_bins"] = requires["bins"]
            if "env" in requires and "requires_env" not in raw:
                raw["requires_env"] = requires["env"]

        # Known fields
        known = {
            "requires_tools",
            "requires_bins",
            "requires_env",
            "always",
            "tags",
            "author",
            "version",
            "extra",
        }
        kwargs: dict[str, Any] = {}
        for key in known:
            if key in raw:
                kwargs[key] = raw.pop(key)

        # Sequence-typed fields accept lists from YAML; coerce defensively
        # at this parse boundary only.
        for seq_key in ("requires_tools", "requires_bins", "requires_env", "tags"):
            value = kwargs.get(seq_key)
            if isinstance(value, list):
                kwargs[seq_key] = tuple(str(v) for v in value)
            elif value is None:
                kwargs.pop(seq_key, None)

        # Whatever remains goes into extra
        extra = kwargs.get("extra", {})
        if not isinstance(extra, dict):
            extra = {}
        if nanobot_prefix:
            extra.update({f"{nanobot_prefix}.{k}": v for k, v in raw.items()})
        else:
            extra.update(raw)
        kwargs["extra"] = extra
        return cls(**kwargs)


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
