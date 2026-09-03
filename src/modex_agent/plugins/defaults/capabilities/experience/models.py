"""Experience value models (plan §10.3 map, §6.1 Pydantic conversions).

Every feature-owned cross-module value is a frozen Pydantic model with
``extra="forbid"``: summaries, full documents, validation results,
curation results, and the catalog's command/result pair. Runtime owners
(catalog, stores, curator, supply) stay regular classes elsewhere in
this package.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ExperienceSummary(BaseModel):
    """Lightweight metadata for injection into system prompts — no body content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    scenario: str = ""
    directory: str = ""


class Experience(ExperienceSummary):
    """Full experience with body content loaded from EXPERIENCE.md."""

    trigger: str = ""
    version: int = 1
    created_at: datetime | None = None
    pinned: bool = False
    location: Path | None = None
    body: str = ""
    frontmatter: dict[str, Any] = Field(default_factory=dict)


class ValidationResult(BaseModel):
    """Result of EXPERIENCE.md format validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CurationResult(BaseModel):
    """Outcome of one curator pass (LRU eviction counts)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checked: int = 0
    evicted: int = 0


# ---------------------------------------------------------------------------
# Catalog command/result pair (plan §10.1 — the concrete deep module's face)
# ---------------------------------------------------------------------------


class ExperienceList(BaseModel):
    """List directories: root (no args), one experience (``name``), or a sub-directory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["list"] = "list"
    name: str | None = None
    path: str | None = None


class ExperienceRead(BaseModel):
    """Read EXPERIENCE.md (default) or a sub-file inside an experience directory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["read"] = "read"
    name: str = ""
    path: str | None = None


class ExperienceWrite(BaseModel):
    """Write content to EXPERIENCE.md (validated) or a sub-file (unvalidated)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["write"] = "write"
    name: str = ""
    content: str = ""
    path: str | None = None


class ExperienceEdit(BaseModel):
    """Edit a file inside an experience directory via find-and-replace."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["edit"] = "edit"
    name: str = ""
    old_string: str = ""
    new_string: str = ""
    path: str | None = None
    replace_all: bool = False


class ExperienceRename(BaseModel):
    """Rename an experience directory; the frontmatter name follows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["rename"] = "rename"
    name: str = ""
    new_name: str = ""


class ExperienceDelete(BaseModel):
    """Delete an experience directory and all its contents permanently."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["delete"] = "delete"
    name: str = ""


type ExperienceCommand = (
    ExperienceList
    | ExperienceRead
    | ExperienceWrite
    | ExperienceEdit
    | ExperienceRename
    | ExperienceDelete
)


class ExperienceResult(BaseModel):
    """One catalog command's LLM-facing outcome.

    ``output`` is the exact string the tool surface returns (raw file-tool
    bytes, or the in-band XML error/validation envelope the atomic tools
    produce). ``ok`` is derived from that protocol: every error path in
    this package formats output as ``<result><status>error</status>…``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    output: str

    @staticmethod
    def from_output(output: str) -> ExperienceResult:
        return ExperienceResult(
            ok=not output.startswith("<result><status>error</status>"),
            output=output,
        )
