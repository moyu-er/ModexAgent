"""Wire models for the scope declaration REST API (ticket 16).

Pydantic request/response models for :mod:`bot.webui.routes.scope_routes`,
mirroring the graph routes' ``graph_models.py`` split. The bill models carry
the compiler's provenance data (``modex_agent.scope.compiler``) verbatim —
per-field source layers, per-tool origins, O3 replacements, and capability
enablement/contribution records — plus the effective values pulled from the
compiled artifacts. Serialization is always ``model_dump(mode="json")`` at
the handler boundary.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.plugins.abc import PluginSource
from modex_agent.scope import (
    CapabilityContributionKind,
    CapabilityGateResult,
    CapabilityState,
    HookOrigin,
    ProvenanceLayer,
    ScopeKind,
    ToolOrigin,
)

# Effective-value union for one bill field: scalar (toolset / registration /
# max_steps), list (tools / capabilities), or the memory override face
# (``MemoryOverrides.model_dump`` — int/bool/None values).
ScopeFieldValue = str | int | list[str] | dict[str, int | bool | None]


class ScopeAgentNode(BaseModel):
    """One declared agent in the topology face."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    parent: str | None
    root: bool


class ScopePoolTopology(BaseModel):
    """One declared pool: its peer links and (flat, parent-referenced) agents."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    peers: list[str] = Field(default_factory=list)
    agents: list[ScopeAgentNode] = Field(default_factory=list)


class ScopeTopologyResponse(BaseModel):
    """The declared scope tree for canvas rendering — either root form
    (workspace-hosted or pool-as-root) without special-casing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ScopeKind
    workspace: str | None
    """Workspace name; ``None`` for a pool-as-root declaration."""
    pools: list[ScopePoolTopology]


class ScopeFieldBill(BaseModel):
    """One field's effective value with its winning source layer (SPEC §3.4
    rule 3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    value: ScopeFieldValue
    layer: ProvenanceLayer
    profile: str | None = None


class ScopeToolBill(BaseModel):
    """One effective tool entry's implementation origin."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    origin: ToolOrigin
    capability: str | None = None
    replaces: str | None = None
    targets: list[str] = Field(default_factory=list)


class ScopeHookBill(BaseModel):
    """One effective hook entry's implementation origin (SPEC §14.8)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hook: str
    origin: HookOrigin
    capability: str | None = None


class ScopeCapabilityContributionBill(BaseModel):
    """One capability contribution with its compile-time gating result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: CapabilityContributionKind
    name: str
    gate: CapabilityGateResult


class ScopeCapabilityBill(BaseModel):
    """One capability's enablement and contribution audit record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: str
    state: CapabilityState
    registration_source: PluginSource | None = None
    contributions: list[ScopeCapabilityContributionBill] = Field(default_factory=list)


class ScopeReplacementBill(BaseModel):
    """One O3 same-name replacement record (``edit ← aci_edit``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_tool: str
    replacement_tool: str
    supplement: str


class ScopeAgentBill(BaseModel):
    """One agent's provenance bill."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pool: str
    agent: str
    root: bool
    fields: list[ScopeFieldBill]
    tools: list[ScopeToolBill]
    hooks: list[ScopeHookBill] = Field(default_factory=list)
    replacements: list[ScopeReplacementBill]
    capabilities: list[ScopeCapabilityBill] = Field(default_factory=list)


class ScopeBillResponse(BaseModel):
    """The whole declaration's bill, in declaration order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agents: list[ScopeAgentBill]


class ScopeDeclarationResponse(BaseModel):
    """The raw declaration YAML (editor surface)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    yaml: str


class ScopeDeclarationUpdateRequest(BaseModel):
    """Editor write-back body — the whole declaration file text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    yaml: str


class ScopeDeclarationSaveResponse(BaseModel):
    """Write-back result. Declaration edits are restart-effective (N2), so
    ``restart_required`` is always true on a successful write."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    saved: bool
    restart_required: bool


__all__ = [
    "ScopeAgentBill",
    "ScopeAgentNode",
    "ScopeBillResponse",
    "ScopeCapabilityBill",
    "ScopeCapabilityContributionBill",
    "ScopeDeclarationResponse",
    "ScopeDeclarationSaveResponse",
    "ScopeDeclarationUpdateRequest",
    "ScopeFieldBill",
    "ScopeFieldValue",
    "ScopePoolTopology",
    "ScopeReplacementBill",
    "ScopeToolBill",
    "ScopeTopologyResponse",
]
