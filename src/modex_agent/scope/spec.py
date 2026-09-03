"""Scope declaration types — flat frozen tree with ``parent`` references.

ADR-0042 / SPEC §3.1 + §3.6. The spec model is FLAT: every agent is an
:class:`AgentSpec` carrying its name and a ``parent`` reference; nested
YAML is parse-level sugar (reversible surface decision) flattened by the
loader. v1 scope kinds are ``workspace`` and ``pool`` only — agents are
pool-internal data, not a scope kind (SPEC N6).

The unified :class:`AgentSpec` replaced the legacy main/sub type split:
one type whose per-node defaults derive from tree POSITION
(:mod:`modex_agent.scope.defaults`), not from type membership — the
legacy split was deleted in ticket 11.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Final, Literal, assert_never

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from modex_agent.core.agent import ExecutionStrategyKind, ProviderKind
from modex_agent.ioc.configs.approval import ApprovalConfig
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.tools.presets import (
    DEFAULT_FORK_MAX_MESSAGES,
    MAX_FORK_MAX_MESSAGES,
    ContextMode,
    ToolPreset,
)

ExecutionStrategyName = ExecutionStrategyKind | str
"""Execution-strategy reference — an :class:`ExecutionStrategyKind` member
or a registry slot component name (same union semantics as the legacy
Main/Sub specs)."""

CapabilityOverride = Literal[False] | dict[str, Any]

_CAPABILITY_NAME_PATTERN: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ScopeKind(StrEnum):
    """Scope kinds (SPEC §3.1). v1: workspace and pool only."""

    WORKSPACE = "workspace"
    POOL = "pool"


def _normalize_execution_strategy_name(
    value: ExecutionStrategyName,
) -> ExecutionStrategyName:
    """Coerce known strategy strings to :class:`ExecutionStrategyKind`.

    Unknown strings pass through verbatim — they name EXECUTION_STRATEGY
    slot components registered by plugins.
    """
    if isinstance(value, ExecutionStrategyKind):
        return value
    try:
        return ExecutionStrategyKind(value)
    except ValueError:
        return value


def _validate_execution_provider_pair(
    execution_strategy: ExecutionStrategyName,
    provider_kind: ProviderKind | None,
) -> None:
    """Enforce ``provider_kind`` set iff ``execution_strategy == EXTERNAL``.

    Same cross-field rule as the legacy Main/Sub specs: an EXTERNAL
    strategy must declare a provider (which CLI to spawn) and no other
    strategy may carry one.
    """
    if execution_strategy == ExecutionStrategyKind.EXTERNAL:
        if provider_kind is None:
            raise ValueError("provider_kind must be set when execution_strategy='external'")
    elif provider_kind is not None:
        raise ValueError(
            "provider_kind must be None when execution_strategy="
            f"{execution_strategy!r} (only 'external' uses a provider)"
        )


class SessionMemoryOverride(BaseModel):
    """Session-layer memory override (legacy subagent ``memory.session`` face)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_context_tokens: int | None = None
    """Session compression threshold override. ``None`` = no override."""


class MemoryDeclaration(BaseModel):
    """Per-node memory override block — SPEC §3.2 memory row's override face.

    Field face is the union of the legacy roster memory blocks: the
    ``MemoryToggle`` archive/core gates (pool.yml main agents) plus the
    subagent session token override (templates). Position fixes the preset
    FAMILY (root — archive/core eligible, non-root — session-only); this block overrides layer toggles within the eligible
    family. The AND gate mirrors ``MemoryToggle``: core memory is fed by
    archive consolidation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    archive_enabled: bool = False
    core_enabled: bool = False
    session: SessionMemoryOverride | None = None

    @model_validator(mode="after")
    def _validate_core_requires_archive(self) -> MemoryDeclaration:
        if self.core_enabled and not self.archive_enabled:
            raise ValueError(
                "core_enabled=True requires archive_enabled=True "
                "(core memory is fed by archive consolidation)"
            )
        return self


class AgentSpec(BaseModel):
    """Unified per-node agent declaration (flat model, ``parent`` reference).

    Field face is the superset of the legacy main/sub roster semantics;
    per-node defaults derive from tree position (SPEC §3.2), never from
    type membership:

    - the legacy per-type tool-preset field is dead — the position-derived
      toolset profile (root → ``full``, non-root → ``read_write``)
      replaces it. A node that deviates declares ``toolset`` explicitly.
    - registration timing defaults to eager (root) / lazy (non-root);
      ``eager`` overrides.
    - ``parent`` is ``None`` exactly for the in-degree-0 node (the root,
      derived — never declared). Uniqueness is enforced by the tree
      validator (ticket 03, V3), not here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    parent: str | None = None
    """Parent agent name within the same pool; ``None`` = root candidate."""

    description: str = ""
    max_steps: int = 100
    use_terminal: bool = False
    terminal_visibility: bool = False
    toolset: ToolPreset | None = None
    """Node-level toolset profile override. ``None`` = position-derived
    default (root → ``full``, non-root → ``read_write``) — the landing
    place of the dead legacy tool-preset values (SPEC §3.4)."""
    tools: list[str] | None = None
    """Explicit tool roster. ``None`` defers to the toolset profile; a
    non-None list replaces the profile's selection."""
    # Open extension payload (rule 14): per-tool config keyed by tool name,
    # typed by ComponentFactory.config_model at assembly time.
    tool_configs: dict[str, dict[str, Any]] | None = None
    capabilities: dict[str, CapabilityOverride] | None = None
    """Capability OVERRIDE MAP, never a whole-set declaration. ``False``
    force-disables a capability and beats auto-apply; a config mapping
    force-enables it. ``{}`` is default config, while an empty outer block is
    equivalent to absence. ``True`` is invalid; use ``{}`` for force-on."""
    hooks: list[str] | None = None
    """Hook roster entries (verbatim, including +/- merge prefixes —
    merging is compiler territory)."""
    # Open extension payload (rule 14): per-hook config keyed by hook name.
    hook_configs: dict[str, dict[str, Any]] | None = None
    approval: ApprovalConfig | None = None
    """Approval config — meaningful only on the root (non-root eligibility
    is refused by position defaults and rejected by V9 in ticket 03)."""
    mcp: list[str] = Field(default_factory=list)
    execution_strategy: ExecutionStrategyName = ExecutionStrategyKind.REACT
    provider_kind: ProviderKind | None = None
    roles: list[str] = Field(default_factory=list)
    prompt_name: str | None = None
    system_prompt: str | None = None
    """File-prompt path sugar, derived into ``file_prompt`` provider config;
    distinct from ``prompt_name``."""
    system_prompt_provider: str | None = None
    # Open heterogeneous payload (rule 14): config for the factory named by
    # system_prompt_provider; validated by its config_model at assembly.
    system_prompt_provider_config: dict[str, Any] = Field(default_factory=dict)
    memory: MemoryDeclaration | None = None
    memory_system: str | None = None
    # Open heterogeneous payload (rule 14): config for the factory named by
    # memory_system; validated by its config_model at assembly.
    memory_system_config: dict[str, Any] = Field(default_factory=dict)
    interceptors: list[str] | None = None
    """Pool-level interceptor roster — declared on the pool root."""
    # Open extension payload (rule 14): per-interceptor config.
    interceptor_configs: dict[str, dict[str, Any]] | None = None
    commands: list[str] | None = None
    """Pool-level command handler roster — declared on the pool root."""
    context_mode: ContextMode = ContextMode.FRESH
    fork_max_messages: int = Field(
        default=DEFAULT_FORK_MAX_MESSAGES, ge=1, le=MAX_FORK_MAX_MESSAGES
    )
    llm_provider: str | None = None
    # Open extension payload (rule 14): provider-specific config.
    llm_provider_config: dict[str, Any] | None = None
    eager: bool | None = None
    """Registration timing override: ``True`` = eager at boot, ``False`` =
    lazy on first dispatch; ``None`` = position-derived default."""

    @property
    def is_root(self) -> bool:
        """Whether this node is the in-degree-0 (root) candidate.

        Root-ness is DERIVED from ``parent``, never declared. V3 (exactly
        one root per pool) is the tree validator's job (ticket 03).
        """
        return self.parent is None

    _normalize_execution_strategy = field_validator("execution_strategy", mode="before")(
        _normalize_execution_strategy_name
    )

    @field_validator("capabilities", mode="before")
    @classmethod
    def _validate_capability_overrides(
        cls,
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Validate override-map syntax only, leaving config keys to compile.

        Capability names are identifiers because dots are reserved for prompt
        section namespacing. ``False`` means force-off; a mapping means
        force-on with config. ``True`` is rejected so each semantic has one
        syntax, and an empty outer mapping has the same meaning as ``None``.
        """
        if not isinstance(value, dict):
            return value
        for name, override in value.items():
            if override is True:
                raise ValueError(
                    f"capability {name!r} override cannot be true; use {{}} "
                    "to force-on with default config or false to force-off"
                )
            if not isinstance(name, str) or _CAPABILITY_NAME_PATTERN.fullmatch(name) is None:
                raise ValueError(
                    f"capability name {name!r} must match "
                    "^[A-Za-z_][A-Za-z0-9_]*$; dots are reserved for "
                    "prompt-section namespacing"
                )
            if override is not False and not isinstance(override, dict):
                raise ValueError(f"capability {name!r} override must be false or a config mapping")
        return value

    @model_validator(mode="after")
    def _validate(self) -> AgentSpec:
        _validate_execution_provider_pair(self.execution_strategy, self.provider_kind)
        return self


class PoolSpec(BaseModel):
    """Pool-layer scope declaration: a flat agents tree + peer links.

    Agents are a FLAT list with ``parent`` references (SPEC §3.6) in
    declaration order; the loader guarantees the parent of every agent
    resolves within this pool. Topology rules (acyclicity, connectivity,
    exactly-one-root, name uniqueness) are the tree validator's job
    (ticket 03, V1-V3/V11).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    agents: list[AgentSpec] = Field(default_factory=list)
    peers: list[str] = Field(default_factory=list)
    """Cross-pool peer links (ADR-0019): root-to-root, same workspace,
    bidirectional. Endpoint/bidirectionality checks are V5 (ticket 03)."""

    @model_validator(mode="after")
    def _validate_parent_refs(self) -> PoolSpec:
        names = {agent.name for agent in self.agents}
        for agent in self.agents:
            if agent.parent is not None and agent.parent not in names:
                raise ValueError(
                    f"agent {agent.name!r} references missing parent "
                    f"{agent.parent!r} in pool {self.name!r}"
                )
        return self

    @property
    def root_agent(self) -> AgentSpec:
        """The pool's root agent (the in-degree-0 node).

        V3 (exactly one root per pool) is the tree validator's guarantee;
        this accessor stays loud on unvalidated input instead of silently
        picking a candidate.
        """
        roots = [agent for agent in self.agents if agent.parent is None]
        if len(roots) != 1:
            raise ValueError(
                f"pool {self.name!r}: expected exactly one root agent "
                f"(parent=None), found {len(roots)}"
            )
        return roots[0]


class WorkspacePersistenceSpec(BaseModel):
    """Workspace memory-backend selection (SPEC §3.1 — 资源选择).

    Ticket 14: the workspace layer's ``persistence.backend`` selects the
    memory/runtime-state backend for the workspace's data (the same
    ``PersistenceBackend`` axis the service-level domain config drives).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: PersistenceBackend


class WorkspacePathsSpec(BaseModel):
    """Workspace path-layout selection (SPEC §3.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    data_dir_name: str = ".modex"


class WorkspaceSpec(BaseModel):
    """Workspace-layer scope declaration: resource selection + hosted pools.

    Ticket 14 completes the resource-selection face (SPEC §3.1): the
    workspace layer selects the memory backend (``persistence``), the path
    layout (``paths``), and the shared MCP server set (``mcp``) — with
    ``pools`` hosting the pool trees. Every selection field is
    ``None = inherit``: an absent field falls back to the service-level
    domain config (``bot_config.yml``), so undeclared deployments keep
    today's data layout (SPEC §3.1 继承父层 + 声明差异).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    persistence: WorkspacePersistenceSpec | None = None
    paths: WorkspacePathsSpec | None = None
    mcp: list[str] | None = None
    """The workspace's shared MCP server set — names referencing
    ``config/mcp/registry.json`` (资源引用, SPEC §3.7). ``None`` = no
    workspace-level set (the full registry remains available)."""
    pools: list[PoolSpec] = Field(default_factory=list)


class ScopeSpec(BaseModel):
    """A loaded scope declaration tree — one of the two root forms.

    SPEC §3.1 "two layers to start": a workspace declaration hosts pools
    (``kind=WORKSPACE``), or a single pool IS the root scope with no
    workspace layer (``kind=POOL``). Exactly one layer is set, matching
    the kind.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ScopeKind
    workspace: WorkspaceSpec | None = None
    pool: PoolSpec | None = None

    @model_validator(mode="after")
    def _validate_form_matches_kind(self) -> ScopeSpec:
        match self.kind:
            case ScopeKind.WORKSPACE:
                if self.workspace is None or self.pool is not None:
                    raise ValueError(
                        "kind='workspace' requires exactly the workspace layer "
                        "(workspace set, pool None)"
                    )
            case ScopeKind.POOL:
                if self.pool is None or self.workspace is not None:
                    raise ValueError(
                        "kind='pool' requires exactly the pool layer (pool set, workspace None)"
                    )
            case unreachable:
                assert_never(unreachable)
        return self
