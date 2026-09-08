"""Frozen subagent permission snapshots, the unified derivation, and denial copy.

``AgentTemplate.materialize`` derives the effective sandbox through
:func:`resolve_agent_sandbox` — the ONE derivation path for every agent:
a declared ``sandbox`` block is authoritative for the permission face
(parallel/exclusive/guard) and validated inside the caller's envelope
(no delegation can amplify), while the substrate face (backend/network/
image) stays with the caller; a missing block inherits the caller
wholesale. A dormant caller still yields guard-only HOST settings, so
native subagents are guarded even where the pool's main agent is not.
Later pool configuration changes do not expand an existing delegation.
Canonicalization may access the filesystem to resolve paths and symlinks.

Native subagents have no human approval channel: denials name the
allowed roots and direct the request to the main session.

HOST shell execution remains available: command guards are not kernel
containment. External provider internal tools bypass framework
interception; snapshot metadata records declared policy and observed
limits, not provider enforcement.

Snapshot assembly belongs to ``multi_agent/template.py``. This module
uses the shared approval audit vocabulary and the canonical workspace
path boundary.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from modex_agent.approval.constants import ApprovalAuditSource
from modex_agent.sandbox.settings import (
    SandboxBackend,
    SandboxSettings,
    WriteSurface,
)
from modex_agent.sandbox.types import EnforcementLevel
from modex_agent.workspace.boundary import PathEnvelope, canonicalize_path

__all__ = [
    "MAX_DELEGATION_DEPTH",
    "DelegationSnapshot",
    "delegation_denial_message",
    "resolve_agent_sandbox",
]


MAX_DELEGATION_DEPTH = 3
"""Recursive delegation budget: root depth is 0; each spawn adds 1.

Task dispatch rejects delegation beyond this limit.
"""


def resolve_agent_sandbox(
    declared: SandboxSettings | None,
    caller: SandboxSettings | None,
    workspace_root: Path,
) -> SandboxSettings:
    """The ONE derivation of any agent's effective sandbox settings.

    - ``declared is None`` → inherit the caller wholesale. A dormant
      caller (``None``) still yields guard-only HOST settings — native
      delegation is guarded even where the pool's main agent is not.
    - ``declared`` is authoritative for the permission face
      (parallel/exclusive/guard); the substrate face (backend, network,
      image) stays with the caller — the pool owns where commands run.
    - Ceiling discipline: every declared ``writable_roots`` entry and
      every ``exclusive.boundaries`` path must resolve inside the
      caller's envelope (workspace + caller's writable_roots) — a
      delegation can only narrow, never amplify. Violations fail fast
      with the offending path.
    """
    base = caller if caller is not None else SandboxSettings()
    if base.backend is SandboxBackend.DEFAULT:
        # Native delegation keeps its guard-only classifier even where the
        # pool's main agent is dormant — normalize the substrate to HOST
        # (no probe, no kernel isolation) so the permission face applies.
        base = base.model_copy(update={"backend": SandboxBackend.HOST})
    if declared is None:
        return base
    ceiling = PathEnvelope(
        (workspace_root, *base.exclusive.writable_roots), base=workspace_root
    )
    declared_roots = (
        *declared.exclusive.writable_roots,
        *(path for boundary in declared.exclusive.boundaries.values() for path in boundary.paths),
    )
    for declared_path in declared_roots:
        if not ceiling.contains(declared_path, base=workspace_root):
            raise ValueError(
                f"declared sandbox path '{declared_path}' resolves outside the "
                f"caller's envelope (workspace {workspace_root} + "
                f"writable_roots {list(base.exclusive.writable_roots)}) — "
                "a delegation can only narrow, never amplify"
            )
    return declared.model_copy(
        update={
            "backend": base.backend,
            "network": base.network,
            "image": base.image,
        }
    )


class DelegationSnapshot(BaseModel):
    """Frozen delegation policy and observed execution capabilities.

    Materialization fixes the workspace root and the derived
    :class:`SandboxSettings` (via :func:`resolve_agent_sandbox`).
    ``backend`` and ``enforcement`` record actual capabilities, or None
    when unresolved or unavailable from an external provider.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_root: Path
    settings: SandboxSettings
    enforcement: EnforcementLevel | None = None
    """Observed kernel enforcement; None means unresolved, never assumed FULL."""

    backend: SandboxBackend | None = None
    """Effective substrate, not the requested tier; None when unknown."""
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

    @property
    def envelope(self) -> tuple[Path, ...]:
        """The declared write envelope, canonicalized against the root.

        workspace joins only under the ``workspace`` surface; under
        ``roots`` the writable_roots ARE the envelope. Anchored once at
        materialization — the frozen snapshot's copy faces.
        """
        root = self.workspace_root
        roots = PathEnvelope(self.settings.exclusive.writable_roots, base=root).roots
        if self.settings.exclusive.write_surface is WriteSurface.WORKSPACE:
            return (root, *roots)
        return roots


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
