"""Sandbox execution value types (frozen pydantic value objects)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnforcementLevel(StrEnum):
    """Honest reporting of the enforcement actually in effect.

    Consumers are telemetry/logging/diagnostics only — never approval
    gating, never silent re-routing (sandbox-integration PRD).
    FULL is a substrate report, not proof that every guard, mount, tool,
    or provider operation is covered or safe.
    """

    FULL = "full"  # Isolated backend reported ready; guard coverage is separate
    PARTIAL = "partial"  # backend ready but some constraints not effective
    NONE = "none"  # No kernel isolation; configured guards/approval may still apply


class SandboxArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    size: int
    mime_type: str


class SandboxResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None  # No exit code available; a timeout may follow execution
    artifacts: list[SandboxArtifact] = Field(default_factory=list)
    execution_time_ms: float = 0.0
    error: str | None = None
    enforcement: EnforcementLevel = EnforcementLevel.NONE
