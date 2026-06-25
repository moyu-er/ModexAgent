from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SkillResource:
    """A resource associated with a skill (e.g., template, sample file)."""

    name: str
    type: str
    path: str | None = None
    description: str = ""


@dataclass
class SkillMetadata:
    """Structured metadata for a skill."""

    requires_tools: list[str] = field(default_factory=list)
    requires_bins: list[str] = field(default_factory=list)
    requires_env: list[str] = field(default_factory=list)
    always: bool = False
    tags: list[str] = field(default_factory=list)
    author: str = ""
    version: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

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


@dataclass
class Skill:
    """A fully loaded skill document."""

    name: str
    description: str = ""
    content: str = ""
    metadata: SkillMetadata = field(default_factory=SkillMetadata)
    source: str = ""
    location: str | None = None
    resources: list[SkillResource] = field(default_factory=list)


@dataclass
class SkillSummary:
    """Lightweight skill descriptor for discovery without loading full content."""

    name: str
    description: str = ""
    metadata: SkillMetadata = field(default_factory=SkillMetadata)
    source: str = ""
    location: str | None = None
    resources: list[SkillResource] = field(default_factory=list)

    def to_skill(self, content: str) -> Skill:
        """Hydrate a full ``Skill`` from this summary plus content."""
        return Skill(
            name=self.name,
            description=self.description,
            content=content,
            metadata=self.metadata,
            source=self.source,
            location=self.location,
            resources=list(self.resources),
        )


@dataclass
class ResolutionContext:
    """Runtime context used when resolving or filtering skills."""

    tool_manager: Any | None = None
    env_vars: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_runtime(cls, tool_manager: Any | None = None) -> ResolutionContext:
        """Create a context from the current process environment."""
        return cls(
            tool_manager=tool_manager,
            env_vars=dict(os.environ),
        )
