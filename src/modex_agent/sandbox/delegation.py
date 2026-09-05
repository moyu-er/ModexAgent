"""Frozen subagent permission snapshots, settings, and denial presentation.

``AgentTemplate.materialize`` captures canonical workspace and allowed roots
once; later pool configuration changes do not expand an existing delegation.
Canonicalization may access the filesystem to resolve paths and symlinks.
Native subagents have no human approval channel: denials name the allowed
roots and direct the request to the main session.

Delegated settings preserve parent READ_ONLY and otherwise restrict known
file targets to workspace + ``allowed_dirs``. HOST shell execution remains
available: command guards are not kernel containment. External provider
internal tools bypass framework interception; snapshot metadata records
declared policy and observed limits, not provider enforcement.

Snapshot assembly belongs to ``multi_agent/template.py``. This module uses
the shared approval audit vocabulary and canonical workspace path boundary.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from modex_agent.approval.constants import ApprovalAuditSource
from modex_agent.sandbox.settings import (
    GuardSettings,
    SandboxBackend,
    SandboxPolicy,
    SandboxSettings,
)
from modex_agent.sandbox.types import EnforcementLevel
from modex_agent.workspace.boundary import canonicalize_path

__all__ = [
    "MAX_DELEGATION_DEPTH",
    "DelegationSnapshot",
    "delegation_denial_message",
    "delegation_sandbox_settings",
]


MAX_DELEGATION_DEPTH = 3
"""Recursive delegation budget: root depth is 0; each spawn adds 1.

Task dispatch rejects delegation beyond this limit.
"""


class DelegationSnapshot(BaseModel):
    """Frozen delegation policy and observed execution capabilities.

    Materialization fixes the workspace root and ``allowed_dirs`` envelope.
    ``requested_backend`` and ``policy`` record declarations; ``backend`` and
    ``enforcement`` record actual capabilities, or None when unresolved or
    unavailable from an external provider.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_root: Path
    allowed_dirs: tuple[Path, ...] = ()
    policy: SandboxPolicy = SandboxPolicy.WORKSPACE_WRITE
    enforcement: EnforcementLevel | None = None
    """Observed kernel enforcement; None means unresolved, never assumed FULL."""

    backend: SandboxBackend | None = None
    """Effective substrate, not the requested tier; None when unknown."""
    requested_backend: SandboxBackend = SandboxBackend.DEFAULT
    file_guards: bool = False
    """Whether framework file-tool checks actually run on this instance."""
    limitations: tuple[str, ...] = ()
    depth: int = 0
    """Root depth is 0; each spawn adds 1, up to MAX_DELEGATION_DEPTH."""

    source: ApprovalAuditSource = Field(default=ApprovalAuditSource.DELEGATION)
    """Audit source shared with ``ApprovalAuditEntry.source``."""

    @field_validator("workspace_root")
    @classmethod
    def _canonical_root(cls, value: Path) -> Path:
        return canonicalize_path(value)

    @field_validator("allowed_dirs")
    @classmethod
    def _canonical_dirs(cls, value: tuple[Path, ...], info: ValidationInfo) -> tuple[Path, ...]:
        return tuple(dict.fromkeys(canonicalize_path(p, base=info.data["workspace_root"]) for p in value))

    @property
    def envelope(self) -> tuple[Path, ...]:
        """Canonical file-access roots; READ_ONLY still forbids writes within them."""
        return (self.workspace_root, *self.allowed_dirs)


def delegation_denial_message(
    tool_name: str,
    target: str,
    snapshot: DelegationSnapshot,
) -> str:
    """Name the denied target and allowed roots, then direct it to the main session.

    Delegated permissions cannot expand from within the subagent session.
    ``target`` is a file path, command, or URL; the allowed roots render
    one per line.
    """
    roots = "\n".join(f"  - {root}" for root in snapshot.envelope)
    return (
        f"This operation is outside the subagent boundary: {tool_name} "
        f"'{target}' is not allowed.\n\n"
        f"Allowed roots:\n{roots}\n\n"
        "The subagent permission scope is fixed at startup and cannot be "
        "expanded from within this session; request this operation in the "
        "main session."
    )


def delegation_sandbox_settings(
    allowed_dirs: tuple[Path, ...],
    *,
    pool: SandboxSettings | None = None,
) -> SandboxSettings:
    """Derive delegated file permissions without disabling HOST execution.

    Keep parent READ_ONLY; otherwise use WORKSPACE_WRITE even for an absent
    or DANGER_FULL_ACCESS parent. Replace extra roots with validated,
    canonical allowed_dirs. DEFAULT maps to guard-only HOST settings, not
    sandbox activation. Shell guards remain best effort; network=False
    cannot isolate HOST networking. External runners record these settings
    as declared policy, not as fabricated provider enforcement.
    """
    if pool is not None:
        return pool.model_copy(
            update={
                "backend": SandboxBackend.HOST if pool.backend is SandboxBackend.DEFAULT else pool.backend,
                "policy": (
                    SandboxPolicy.READ_ONLY
                    if pool.policy is SandboxPolicy.READ_ONLY
                    else SandboxPolicy.WORKSPACE_WRITE
                ),
                "writable_roots": list(allowed_dirs),
            }
        )
    return SandboxSettings(
        backend=SandboxBackend.HOST,
        policy=SandboxPolicy.WORKSPACE_WRITE,
        network=False,
        writable_roots=list(allowed_dirs),
        guard=GuardSettings(),
    )
