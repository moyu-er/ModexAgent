"""ScopeCompiler — tree → per-agent AssemblySpecs + effective toolsets +
O3 accounting (SPEC §3.2/§3.4/§5.2, ticket 06).

A pure-function compiler over a validated
:class:`~modex_agent.scope.spec.ScopeSpec` tree: same inputs →
byte-identical outputs (the ticket-18 hash input). Zero side effects, zero
IO — YAML loading lives in the loader; the profile store and the
:class:`~modex_agent.workspace.context.WorkspaceContext` runtime object
arrive as parameters.

Outputs (three per declared agent, in declaration order):

- **AssemblySpec** — the assembly input face. The derivation core (preset
  expansion, tools/hooks merge, system-prompt sugar, agent-type
  derivation) lives in ``scope.derivation`` — the compiler consumes the
  shared functions rather than reimplementing them (converge, don't
  duplicate). Two deliberate conventions, both pinned by the split-brain
  test: the §5.2 derived communication entries are injected into
  ``tools``, and ``pool_name`` keeps the legacy convention of the ROOT
  AGENT'S NAME (set from the pool's root agent).
- **Effective toolset** — the V6 input face: the derived ``spec.tools``
  including the injected communication entries (SPEC §5.2: the effective
  toolset V6 checks IS the derived spec.tools).
- **Per-field provenance** — the bill data (ticket 16): every field's
  winning layer (framework default / profile / local), every tool entry's
  origin, and the O3 same-name replacement records (a capability's product
  replacing a default tool entry).

Derived communication entries (SPEC §5.2/§8.4): the tree derivation is
CAPABILITY-CONTRIBUTED — a capability's ``contribute`` declares
``derived_tools`` (tool name + origin + targets from the tree facts it
holds) and the compiler routes them through its generic derived-entry
machinery (merge-base position + origin/targets classification). The
compiler knows no capability and no tree-derivation rule; the shipped
communication entries come from the FW-bundled communication capability
registered in the CAPABILITY slot, and the TOOL-slot FW factories in
``plugins/defaults/communication.py`` resolve the entry names at
assembly time.

An explicitly declared unprefixed ``tools`` list — local or profile — is
wholesale (O4/V8): it REPLACES the preset-derived base including the
derived entries, and V6 catches child-carrying agents that drop ``task``.
Incremental ``+/-`` lists merge over the base (derived entries included,
so ``-task`` is expressible and V6-guarded).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.multi_agent.execution_strategy import strategy_name_of
from modex_agent.plugins.abc import ComponentSlot, PluginSource
from modex_agent.plugins.assembly.spec import AssemblySpec, MemoryOverrides
from modex_agent.plugins.capability import (
    AgentDeclarationView,
    AgentDeclaredFields,
    Capability,
    CapabilityBinding,
    CapabilityContribution,
    ChildSummary,
    CompiledCapability,
    DerivedToolSpec,
    FinalRosterView,
    ToolReplacementSpec,
    TreePositionView,
)
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope.defaults import (
    PositionDefaults,
    defaults_for_position,
    effective_defaults,
    position_default_hooks,
)
from modex_agent.scope.derivation import (
    _DEFAULT_LLM_PROVIDER,
    _derive_agent_type,
    _expand_preset_tool_names,
    _expand_system_prompt,
    _merge_hooks,
    _merge_tools,
    strip_add_prefix,
)
from modex_agent.scope.profile import (
    STANDARD_PROFILES,
    Profile,
    ProfileStore,
    merge_memory_declarations,
)
from modex_agent.scope.spec import AgentSpec, MemoryDeclaration, PoolSpec, ScopeSpec
from modex_agent.scope.validator import EffectiveAgentConfig, _pools_of
from modex_agent.workspace.context import WorkspaceContext


class ProvenanceLayer(StrEnum):
    """The three resolution layers of the bill (SPEC §3.4 rule 3)."""

    FRAMEWORK = "framework"
    """Model defaults + the SPEC §3.2 position-derived defaults table."""
    PROFILE = "profile"
    """The bound named profile."""
    LOCAL = "local"
    """The node's own declaration."""


class ToolOrigin(StrEnum):
    """Where one effective tool entry came from."""

    PRESET = "preset"
    """Framework toolset preset expansion."""
    PROFILE_TOOLS = "profile_tools"
    """Wholesale tools list from the bound profile."""
    LOCAL_TOOLS = "local_tools"
    """Wholesale or incremental local ``tools:`` declaration."""
    SUPPLEMENT = "supplement"
    """Legacy classification retained for pre-capability migration goldens."""
    CAPABILITY_DERIVED = "capability_derived"
    """Non-derived tool contributed by a named capability."""
    DERIVED_TASK = "derived_task"
    DERIVED_SEND_TO_AGENT = "derived_send_to_agent"
    DERIVED_SEND_TO_PEER = "derived_send_to_peer"


class HookOrigin(StrEnum):
    """Where one effective hook entry came from."""

    POSITION_DEFAULT = "position_default"
    """The SPEC §3.2 position-default hook rows (framework base)."""
    CAPABILITY_DERIVED = "capability_derived"
    """Hook name contributed by a named capability."""
    LOCAL_HOOKS = "local_hooks"
    """The node's own ``hooks:`` declaration."""


class FieldProvenance(BaseModel):
    """One field's winning layer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    """AgentSpec field name whose effective value this records."""
    layer: ProvenanceLayer
    profile: str | None = None
    """Bound profile name when ``layer`` is PROFILE."""


class HookEntryProvenance(BaseModel):
    """One effective hook entry's origin (SPEC §14.8 zero-unsourced)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hook: str
    origin: HookOrigin
    capability: str | None = None
    """Contributing capability name for CAPABILITY_DERIVED entries."""


class ToolEntryProvenance(BaseModel):
    """One effective tool entry's origin."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    origin: ToolOrigin
    capability: str | None = None
    """Contributing capability name for CAPABILITY_DERIVED entries."""
    replaces: str | None = None
    """O3: the default tool entry this entry replaced."""
    targets: list[str] = Field(default_factory=list)
    """Derived communication entries only: task → direct child agents;
    send_to_agent → ``(parent,)``; send_to_peer → peer pool names."""


class ToolReplacement(BaseModel):
    """One O3 same-name replacement record (SPEC §3.5): a capability's
    product replaced a default tool entry in the effective toolset
    (the ``replaced_tool ← replacement_tool`` pattern). Per-agent
    granularity — the effective capability set decides, unlike O2's
    global registry priority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_tool: str
    replacement_tool: str
    capability: str
    """The capability registration name whose contribution declared the
    replacement (B7 — the compile product references capabilities by
    registration name, never by a retired enum)."""


class CapabilityState(StrEnum):
    """C0 enablement outcome recorded in an agent's bill."""

    AUTO = "auto"
    DECLARED = "declared"
    VETOED = "vetoed"


class CapabilityContributionKind(StrEnum):
    """Component category contributed by a capability."""

    TOOL = "tool"
    HOOK = "hook"
    SECTION = "section"


class CapabilityGateResult(StrEnum):
    """Whether one C1 contribution survived merge and C2 gating."""

    VOUCHED = "vouched"
    DROPPED = "dropped"


class CapabilityContributionProvenance(BaseModel):
    """One capability contribution and its final gating result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: CapabilityContributionKind
    name: str
    gate: CapabilityGateResult


class CapabilityProvenance(BaseModel):
    """One capability's C0 state, registration source, and C1/C2 audit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: str
    state: CapabilityState
    registration_source: PluginSource | None = None
    contributions: list[CapabilityContributionProvenance] = Field(default_factory=list)


class AgentProvenance(BaseModel):
    """One agent's field, tool, replacement, and capability bill data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pool: str
    agent: str
    fields: list[FieldProvenance]
    tools: list[ToolEntryProvenance]
    replacements: list[ToolReplacement]
    capabilities: list[CapabilityProvenance] = Field(default_factory=list)
    hooks: list[HookEntryProvenance] = Field(default_factory=list)

    def replacement_of(self, default_tool: str) -> ToolReplacement | None:
        """The O3 replacement record covering a default tool name, if any."""
        return next(
            (r for r in self.replacements if r.default_tool == default_tool),
            None,
        )


class CompiledAgent(BaseModel):
    """One agent's compiled artifacts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec: AssemblySpec
    effective: EffectiveAgentConfig
    defaults: PositionDefaults
    """Resolved position defaults (memory family, registration timing,
    toolset profile) — the ticket-07/09 wiring input."""
    provenance: AgentProvenance


class ScopeCompilation(BaseModel):
    """ScopeCompiler output — per-agent artifacts in declaration order
    (pools in workspace declaration order, agents in pool declaration
    order)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agents: list[CompiledAgent]


def compile_scope(
    spec: ScopeSpec,
    *,
    workspace_ctx: WorkspaceContext,
    profiles: ProfileStore = STANDARD_PROFILES,
    default_llm_provider: str = _DEFAULT_LLM_PROVIDER,
    registry: ComponentRegistry | None = None,
) -> ScopeCompilation:
    """Compile a validated declaration tree into per-agent artifacts.

    Args:
        spec: the (phase-1 validated) declaration tree.
        workspace_ctx: runtime object threaded into every AssemblySpec
            (excluded from byte-stability comparisons).
        profiles: the profile store supplying the profile layer.
        default_llm_provider: fallback LLM provider component name (the
            ``default`` factory).
        registry: the ComponentRegistry supplying the CAPABILITY slot for
            the compile-time capability protocol (C0/C1/C2, SPEC §6).
            ``None`` DISABLES capability resolution: a tree with no
            declared capabilities compiles exactly as before, while any
            declared capability raises loudly instead of being silently
            ignored.

    Returns:
        One :class:`CompiledAgent` per declared agent, in declaration
        order. Raises ``ValueError`` for a pool without exactly one root —
        the compiler requires a validated tree (V3).
    """
    if registry is None and _declares_capabilities(spec):
        raise ValueError(
            "registry required when capabilities are declared: pass the "
            "ComponentRegistry used at boot (compile_scope registry=None "
            "disables capability resolution)"
        )
    compiled: list[CompiledAgent] = []
    for pool in _pools_of(spec):
        compiled.extend(
            _compile_pool(
                pool,
                profiles=profiles,
                workspace_ctx=workspace_ctx,
                default_llm_provider=default_llm_provider,
                registry=registry,
            )
        )
    return ScopeCompilation(agents=compiled)


def _compile_pool(
    pool: PoolSpec,
    *,
    profiles: ProfileStore,
    workspace_ctx: WorkspaceContext,
    default_llm_provider: str,
    registry: ComponentRegistry | None,
) -> list[CompiledAgent]:
    roots = [agent.name for agent in pool.agents if agent.parent is None]
    if len(roots) != 1:
        raise ValueError(
            f"pool {pool.name!r}: expected exactly one root agent (V3), "
            f"found {len(roots)} — compile_scope requires a validated tree"
        )
    children: dict[str, list[str]] = {}
    for agent in pool.agents:
        if agent.parent is not None:
            children.setdefault(agent.parent, []).append(agent.name)
    return [
        _compile_agent(
            pool,
            agent,
            root_name=roots[0],
            children=children,
            profiles=profiles,
            workspace_ctx=workspace_ctx,
            default_llm_provider=default_llm_provider,
            registry=registry,
        )
        for agent in pool.agents
    ]


def _compile_agent(
    pool: PoolSpec,
    agent: AgentSpec,
    *,
    root_name: str,
    children: dict[str, list[str]],
    profiles: ProfileStore,
    workspace_ctx: WorkspaceContext,
    default_llm_provider: str,
    registry: ComponentRegistry | None,
) -> CompiledAgent:
    is_root = agent.parent is None
    local_fields = agent.model_fields_set

    # ── three-layer resolution: framework ← profile ← local ──────────────
    # The toolset preset (local declaration over the position default) names
    # the bound profile; the profile's remaining fields form the profile
    # layer between the framework defaults and the local declaration.
    toolset = (
        agent.toolset
        if agent.toolset is not None
        else defaults_for_position(is_root=is_root).toolset_profile
    )
    toolset_layer = (
        ProvenanceLayer.LOCAL if agent.toolset is not None else ProvenanceLayer.FRAMEWORK
    )
    bound = profiles.get(toolset.value)
    profile_tools = bound.tools if bound is not None else None
    profile_eager = bound.eager if bound is not None else None
    profile_max_steps = bound.max_steps if bound is not None else None
    profile_memory = bound.memory if bound is not None else None

    tools_list, tools_layer = _layered(agent.tools, profile_tools)
    # No profile face: the override map is purely local, and an empty
    # outer block is equivalent to absence (SPEC §5.1).
    capabilities_layer = ProvenanceLayer.LOCAL if agent.capabilities else ProvenanceLayer.FRAMEWORK
    eager, eager_layer = _layered(agent.eager, profile_eager)
    max_steps, max_steps_layer = _layered(
        agent.max_steps if "max_steps" in local_fields else None,
        profile_max_steps,
    )
    if max_steps is None:
        max_steps = agent.max_steps  # framework model default
    merged_memory = merge_memory_declarations(profile_memory, agent.memory)
    if agent.memory is not None:
        memory_layer = ProvenanceLayer.LOCAL
    elif profile_memory is not None:
        memory_layer = ProvenanceLayer.PROFILE
    else:
        memory_layer = ProvenanceLayer.FRAMEWORK

    defaults = effective_defaults(
        agent.model_copy(update={"toolset": toolset, "eager": eager, "memory": merged_memory})
    )

    # ── tools pipeline: preset expansion + derived entries + O3 ──────────
    preset_names = _expand_preset_tool_names(toolset)

    # ── capability protocol: C0 enablement → C1 contribution (SPEC §6) ──
    # Contributes BEFORE the roster merge so the component-level veto
    # (``tools: [-x]`` / ``hooks: [-y]``) applies to capability
    # contributions — the generalized name-merge semantics: a contributed
    # tool name enters the merge base exactly like a preset name, so
    # ``+/-`` entries and unprefixed wholesale replaces control it.
    # With registry=None (and no declarations — checked upfront in
    # compile_scope) nothing here runs and the compile product is
    # byte-identical to the pre-capability compiler.
    capability_tool_owners: dict[str, str] = {}
    capability_hooks: list[str] = []
    # Contributed hook name → the capability names that contributed it
    # (C1 record feeding the post-bind hook gating below).
    contributed_hook_owners: dict[str, list[str]] = {}
    # Contributed hook name → first contributing capability (the bill's
    # single-owner face, mirroring capability_tool_owners).
    capability_hook_owners: dict[str, str] = {}
    capability_replacement_specs: list[tuple[str, ToolReplacementSpec]] = []
    capability_states: list[tuple[str, Capability, BaseModel, CapabilityContribution]] = []
    capability_provenance: list[CapabilityProvenance] = []
    # Tree-derived entries (SPEC §8.4 A3): contributed specs in C1
    # registry-enumeration order, deduped by tool name (first
    # contribution wins). They ride the derived-entry machinery — the
    # same base position and origin+targets classification the retired
    # hardcoded tree derivation produced.
    derived_specs: list[DerivedToolSpec] = []
    tree_view = TreePositionView(
        pool_name=pool.name,
        agent_name=agent.name,
        is_root=is_root,
        parent=agent.parent,
        children=_child_summaries(pool, children.get(agent.name, [])),
        peers=tuple(pool.peers),
    )
    if registry is not None:
        effective_capabilities, capability_provenance = _effective_capabilities(
            agent, tree_view, registry=registry
        )
        for name, override_config in effective_capabilities:
            capability = registry.resolve_capability(name)  # V13: ComponentNotFoundError
            config = capability.config_model.model_validate(override_config)
            contribution = capability.contribute(tree_view, config)
            capability_states.append((name, capability, config, contribution))
            capability_replacement_specs.extend(
                (name, spec) for spec in contribution.tool_replacements
            )
            for tool_name in contribution.tools:
                capability_tool_owners.setdefault(tool_name, name)
            for hook_name in contribution.hooks:
                if hook_name not in capability_hooks:
                    capability_hooks.append(hook_name)
                contributed_hook_owners.setdefault(hook_name, []).append(name)
                capability_hook_owners.setdefault(hook_name, name)
            for derived_spec in contribution.derived_tools:
                if derived_spec.tool not in {spec.tool for spec in derived_specs}:
                    derived_specs.append(derived_spec)

    # The derived-entry provenance face: the compiler maps each capability
    # declared origin onto the identically-valued ToolOrigin member —
    # value-preserving and capability-agnostic (an origin with no
    # ToolOrigin counterpart fails the compile loudly).
    derived = [
        ToolEntryProvenance(
            tool=spec.tool,
            origin=ToolOrigin(spec.origin.value),
            targets=list(spec.targets),
        )
        for spec in derived_specs
    ]
    base = preset_names + [entry.tool for entry in derived]
    base += [name for name in capability_tool_owners if name not in base]
    merged_tools = _merge_tools(base, tools_list)
    declared_origin = (
        ToolOrigin.LOCAL_TOOLS if agent.tools is not None else ToolOrigin.PROFILE_TOOLS
    )
    tool_provenance = _classify_tools(
        merged_tools,
        derived=derived,
        preset_names=preset_names,
        tools_list=tools_list,
        declared_origin=declared_origin,
        capability_tool_owners=capability_tool_owners,
    )
    final_tools = merged_tools
    replacements: list[ToolReplacement] = []
    # O3 capability tool replacements — applied POST-merge at the pipeline
    # position the historical supplement special case occupied (see
    # ``_apply_capability_replacements``).
    if capability_replacement_specs:
        final_tools, tool_provenance, replacements = _apply_capability_replacements(
            final_tools, tool_provenance, replacements, capability_replacement_specs
        )

    # Position-default hooks (SPEC §3.2 hook rows) enter the merge base
    # ahead of capability contributions and the node's declaration — the
    # hook face of the preset tool names, so ``hooks: [-name]`` vetoes a
    # default and a declared ``+name`` dedups against it. External agents
    # take no native hook face (the structural exclusion mirroring V12's
    # capability exclusion), so their roster stays declaration-only.
    position_hooks = (
        list(position_default_hooks(is_root=is_root)) if agent.provider_kind is None else []
    )
    hooks_input: list[str] | None = agent.hooks
    if capability_hooks or position_hooks:
        hooks_input = position_hooks + capability_hooks + (agent.hooks or [])
    merged_hooks = _merge_hooks(hooks_input)

    # ── C2 binding (SPEC §6): post-merge anchor validation ──────────────
    # bind() sees the FINAL rosters; a CapabilityError propagates as a
    # boot failure. The compile product carries only frozen data —
    # capability objects never enter it. After every bind, contributed
    # hooks are gated against their capability's binding (generic anchor
    # semantics — the compiler stays capability-agnostic).
    capabilities_block: tuple[CompiledCapability, ...] = ()
    if capability_states:
        roster_view = FinalRosterView(tools=tuple(final_tools), hooks=tuple(merged_hooks))
        bindings: dict[str, CapabilityBinding] = {}
        for name, capability, config, _contribution in capability_states:
            binding = capability.bind(tree_view, config, roster_view)
            bindings[name] = binding
        merged_hooks = _gate_contributed_hooks(
            merged_hooks, agent.hooks, contributed_hook_owners, bindings
        )
        capabilities_block = tuple(
            CompiledCapability(name=name, config=config.model_dump(), binding=bindings[name])
            for name, _capability, config, _contribution in capability_states
        )
        contribution_audit = {
            name: _capability_contribution_provenance(
                contribution,
                bindings[name],
                FinalRosterView(tools=tuple(final_tools), hooks=tuple(merged_hooks)),
            )
            for name, _capability, _config, contribution in capability_states
        }
        capability_provenance = [
            entry.model_copy(update={"contributions": contribution_audit.get(entry.capability, [])})
            for entry in capability_provenance
        ]

    hook_provenance = _classify_hooks(
        merged_hooks,
        position_hooks=position_hooks,
        capability_owners=capability_hook_owners,
    )
    hooks_layer = ProvenanceLayer.LOCAL if agent.hooks is not None else ProvenanceLayer.FRAMEWORK

    system_prompt_provider, system_prompt_config = _expand_system_prompt(
        agent.system_prompt,
        agent.system_prompt_provider,
        agent.system_prompt_provider_config,
        agent.prompt_name,
        agent.name,
    )
    spec = AssemblySpec(
        agent_type=_derive_agent_type(is_root, agent.provider_kind),
        agent_name=agent.name,
        pool_name=root_name,
        description=agent.description,
        max_iterations=max_steps,
        roles=list(agent.roles),
        tools=final_tools,
        tool_configs=dict(agent.tool_configs or {}),
        hooks=merged_hooks,
        hook_configs=dict(agent.hook_configs or {}),
        llm_provider=agent.llm_provider or default_llm_provider,
        llm_provider_config=dict(agent.llm_provider_config or {}),
        system_prompt_provider=system_prompt_provider,
        system_prompt_config=system_prompt_config,
        memory_overrides=_memory_overrides(merged_memory, is_root=is_root),
        memory_system=agent.memory_system,
        memory_system_config=dict(agent.memory_system_config),
        execution_strategy=strategy_name_of(agent.execution_strategy),
        provider_kind=(agent.provider_kind.value if agent.provider_kind is not None else None),
        mcp_servers=list(agent.mcp),
        # The `+` prefix is declaration sugar (incremental-merge face); the
        # factory resolution and interceptor_configs lookup key on the bare
        # name — strip through the single strip authority like hooks/tools.
        interceptors=[strip_add_prefix(entry) for entry in (agent.interceptors or [])] if is_root else [],
        interceptor_configs=dict(agent.interceptor_configs or {}) if is_root else {},
        commands=list(agent.commands) if is_root and agent.commands is not None else None,
        capabilities=capabilities_block,
        workspace_ctx=workspace_ctx,
    )
    return CompiledAgent(
        spec=spec,
        effective=EffectiveAgentConfig(pool=pool.name, agent=agent.name, tools=list(final_tools)),
        defaults=defaults,
        provenance=AgentProvenance(
            pool=pool.name,
            agent=agent.name,
            fields=[
                FieldProvenance(field="toolset", layer=toolset_layer),
                FieldProvenance(
                    field="tools",
                    layer=tools_layer,
                    profile=_profile_name(bound, tools_layer),
                ),
                FieldProvenance(field="capabilities", layer=capabilities_layer),
                FieldProvenance(field="hooks", layer=hooks_layer),
                FieldProvenance(
                    field="eager",
                    layer=eager_layer,
                    profile=_profile_name(bound, eager_layer),
                ),
                FieldProvenance(
                    field="max_steps",
                    layer=max_steps_layer,
                    profile=_profile_name(bound, max_steps_layer),
                ),
                FieldProvenance(
                    field="memory",
                    layer=memory_layer,
                    profile=_profile_name(bound, memory_layer),
                ),
                *(
                    [FieldProvenance(field="sandbox", layer=ProvenanceLayer.LOCAL)]
                    if agent.sandbox is not None
                    else []
                ),
            ],
            tools=tool_provenance,
            replacements=replacements,
            capabilities=capability_provenance,
            hooks=hook_provenance,
        ),
    )


# ─── Internal helpers ──────────────────────────────────────────────────────


def _layered[T](local: T | None, profile_value: T | None) -> tuple[T | None, ProvenanceLayer]:
    """``local ?? profile ?? framework`` for optional faces; the caller
    supplies the framework fallback for the None (framework) case."""
    if local is not None:
        return local, ProvenanceLayer.LOCAL
    if profile_value is not None:
        return profile_value, ProvenanceLayer.PROFILE
    return None, ProvenanceLayer.FRAMEWORK


def _profile_name(bound: Profile | None, layer: ProvenanceLayer) -> str | None:
    """The bound profile's name when this field's layer is PROFILE."""
    if layer is ProvenanceLayer.PROFILE and bound is not None:
        return bound.name
    return None


def _declares_capabilities(spec: ScopeSpec) -> bool:
    """Whether ANY agent in the tree declares a non-empty capabilities block.

    An empty override map is equivalent to absence (SPEC §5.1), so only a
    non-empty block counts as a declaration.
    """
    return any(agent.capabilities for pool in _pools_of(spec) for agent in pool.agents)


def _child_summaries(pool: PoolSpec, child_names: list[str]) -> tuple[ChildSummary, ...]:
    """Direct-child summaries for the capability tree views, in declaration
    order."""
    descriptions = {agent.name: agent.description for agent in pool.agents}
    return tuple(ChildSummary(name=name, description=descriptions[name]) for name in child_names)


def _declared_fields_of(agent: AgentSpec) -> AgentDeclaredFields:
    """Project an AgentSpec's declared primitive fields onto the C0 predicate
    face — enum fields to their string values, None-ability mirroring
    AgentSpec field-for-field."""
    return AgentDeclaredFields(
        toolset=agent.toolset.value if agent.toolset is not None else None,
        tools=list(agent.tools) if agent.tools is not None else None,
        hooks=list(agent.hooks) if agent.hooks is not None else None,
        mcp=list(agent.mcp),
        use_terminal=agent.use_terminal,
        execution_strategy=strategy_name_of(agent.execution_strategy),
        provider_kind=(agent.provider_kind.value if agent.provider_kind is not None else None),
        eager=agent.eager,
        roles=list(agent.roles),
        description=agent.description,
    )


def _effective_capabilities(
    agent: AgentSpec,
    tree_view: TreePositionView,
    *,
    registry: ComponentRegistry,
) -> tuple[list[tuple[str, dict[str, Any]]], list[CapabilityProvenance]]:
    """C0: one agent's effective capabilities as an ordered
    ``(name, override config)`` list (SPEC §3.2).

    effective = auto ∆ declared overrides — a ``False`` override removes an
    auto-applied capability; a mapping override force-enables (with config,
    replacing the default). NATIVE agents run every registry-enumerated
    predicate against the declaration view; EXTERNAL agents skip predicates
    entirely — a non-empty declared block is a loud error (defense in
    depth behind phase-1 V12), never a silently-ignored declaration.

    Order: registry enumeration order (deterministic across processes —
    never set iteration); declared-but-unregistered names follow sorted at
    the end so resolving them raises ``ComponentNotFoundError`` (V13, one
    compile cycle before slot late-binding).
    """
    overrides = agent.capabilities or {}
    if agent.provider_kind is not None:
        if overrides:
            raise ValueError(
                f"pool {tree_view.pool_name!r}: external agent {agent.name!r} "
                "declares capabilities — external agents take no native "
                "component face; remove the capabilities block (V12, "
                "defense in depth behind the phase-1 validator)"
            )
        return [], []
    view = AgentDeclarationView(
        pool_name=tree_view.pool_name,
        agent_name=tree_view.agent_name,
        is_root=tree_view.is_root,
        parent=tree_view.parent,
        children=tree_view.children,
        peers=tree_view.peers,
        declared=_declared_fields_of(agent),
    )
    registered = registry.names(ComponentSlot.CAPABILITY)
    # Open config payload (rule 14): capability configs are open — keys
    # are capability-private and validated by each config_model at C1.
    enabled: dict[str, dict[str, Any]] = {}
    provenance: list[CapabilityProvenance] = []
    for name in registered:
        auto_applies = registry.resolve_capability(name).applies(view)
        source = registry.registration_source(ComponentSlot.CAPABILITY, name)
        if name in overrides:
            override = overrides[name]
            if override is False:
                if auto_applies:
                    provenance.append(
                        CapabilityProvenance(
                            capability=name,
                            state=CapabilityState.VETOED,
                            registration_source=source,
                        )
                    )
                continue
            enabled[name] = dict(override)
            provenance.append(
                CapabilityProvenance(
                    capability=name,
                    state=CapabilityState.DECLARED,
                    registration_source=source,
                )
            )
        elif auto_applies:
            enabled[name] = {}
            provenance.append(
                CapabilityProvenance(
                    capability=name,
                    state=CapabilityState.AUTO,
                    registration_source=source,
                )
            )
    for name in sorted(name for name in overrides if name not in registered):
        override = overrides[name]
        if override is False:
            continue
        enabled[name] = dict(override)
        provenance.append(
            CapabilityProvenance(
                capability=name,
                state=CapabilityState.DECLARED,
                registration_source=None,
            )
        )
    ordered = [name for name in registered if name in enabled]
    ordered += sorted(name for name in enabled if name not in registered)
    return [(name, enabled[name]) for name in ordered], provenance


def _capability_contribution_provenance(
    contribution: CapabilityContribution,
    binding: CapabilityBinding,
    final: FinalRosterView,
) -> list[CapabilityContributionProvenance]:
    """Project one capability's C1 entries onto their final C2 bill state."""
    result = [
        CapabilityContributionProvenance(
            kind=CapabilityContributionKind.TOOL,
            name=name,
            gate=(
                CapabilityGateResult.VOUCHED
                if name in final.tools
                else CapabilityGateResult.DROPPED
            ),
        )
        for name in contribution.tools
    ]
    result.extend(
        CapabilityContributionProvenance(
            kind=CapabilityContributionKind.TOOL,
            name=spec.tool,
            gate=(
                CapabilityGateResult.VOUCHED
                if spec.tool in final.tools
                else CapabilityGateResult.DROPPED
            ),
        )
        for spec in contribution.derived_tools
    )
    result.extend(
        CapabilityContributionProvenance(
            kind=CapabilityContributionKind.HOOK,
            name=name,
            gate=(
                CapabilityGateResult.VOUCHED
                if name in binding.hooks and name in final.hooks
                else CapabilityGateResult.DROPPED
            ),
        )
        for name in contribution.hooks
    )
    result.extend(
        CapabilityContributionProvenance(
            kind=CapabilityContributionKind.SECTION,
            name=section.section_id,
            gate=(
                CapabilityGateResult.VOUCHED
                if section in binding.active_sections
                else CapabilityGateResult.DROPPED
            ),
        )
        for section in contribution.sections
    )
    return result


def _gate_contributed_hooks(
    merged_hooks: list[str],
    declared_hooks: list[str] | None,
    contributed_hook_owners: dict[str, list[str]],
    bindings: Mapping[str, CapabilityBinding],
) -> list[str]:
    """Generic post-bind hook gating (SPEC §3.3 anchor semantics).

    A hook name some capability CONTRIBUTED survives in ``merged_hooks``
    iff at least one contributing capability's binding vouches for it
    (lists it in ``binding.hooks``); unvouched contributed names are
    removed — the binding is the authority for a capability's non-tool
    components once its anchors are known.

    Boundaries (capability-agnostic):

    - Only CONTRIBUTED names are gateable — a purely handwritten roster
      entry (no contributing capability) is never touched.
    - A handwritten ``+name``/bare ``name`` declaration keeps a
      contributed name alive even when its capability dropped it: that
      entry belongs to the declaration, not the binding.
    - Gating only removes, never adds — a minus-vetoed contributed name
      stays out regardless of vouching (the merge already removed it).
    """
    if not contributed_hook_owners:
        return merged_hooks
    declared_adds = {
        strip_add_prefix(entry)
        for entry in (declared_hooks or [])
        if entry and not entry.startswith("-")
    }
    result = list(merged_hooks)
    for hook_name, owners in contributed_hook_owners.items():
        if any(hook_name in bindings[owner].hooks for owner in owners if owner in bindings):
            continue
        if hook_name in declared_adds:
            continue
        if hook_name in result:
            result.remove(hook_name)
    return result


def _apply_capability_replacements(
    tools: list[str],
    entries: list[ToolEntryProvenance],
    replacements: list[ToolReplacement],
    specs: list[tuple[str, ToolReplacementSpec]],
) -> tuple[list[str], list[ToolEntryProvenance], list[ToolReplacement]]:
    """Apply capability tool replacements (O3) post-merge — the generic
    form of the historical supplement special case that ran at this exact
    pipeline position.

    Per replacement declaration ``(capability_name, spec)``, applied in
    C1 registry-enumeration order:

    - the replaced default entry dies (all occurrences);
    - the replacement entry lands at the END of the final roster (moved
      there if the merge-base contribution already carried it);
    - a ``ToolReplacement`` provenance record is appended iff the default
      entry was present in the roster;
    - the replacement tool's classified provenance entry (merge-base
      contribution) is REPLACED by a CAPABILITY_DERIVED entry annotated
      with the capability and replaced default name, at the end of the entry list — the
      replaced default's own classified entry stays in place (the audit
      trail keeps both sides of the swap).

    Roster-order note (recorded divergence, plan todo 7): the historical
    branch interleaved with other supplement append paths by declared
    order; the generic application always lands the replacement entry at
    the roster tail. Name set and replacement records are unchanged.
    """
    result = list(tools)
    provenance = list(entries)
    records = list(replacements)
    for capability_name, spec in specs:
        had_default = spec.replaced_tool in result
        result = [name for name in result if name != spec.replaced_tool]
        if spec.replacement_tool in result:
            result = [name for name in result if name != spec.replacement_tool]
        result.append(spec.replacement_tool)
        if had_default:
            records.append(
                ToolReplacement(
                    default_tool=spec.replaced_tool,
                    replacement_tool=spec.replacement_tool,
                    capability=capability_name,
                )
            )
        provenance = [entry for entry in provenance if entry.tool != spec.replacement_tool]
        provenance.append(
            ToolEntryProvenance(
                tool=spec.replacement_tool,
                origin=ToolOrigin.CAPABILITY_DERIVED,
                capability=capability_name,
                replaces=spec.replaced_tool if had_default else None,
            )
        )
    return result, provenance, records


def _classify_tools(
    merged_tools: list[str],
    *,
    derived: list[ToolEntryProvenance],
    preset_names: list[str],
    tools_list: list[str] | None,
    declared_origin: ToolOrigin,
    capability_tool_owners: Mapping[str, str],
) -> list[ToolEntryProvenance]:
    """Classify pre-replacement tool entries by origin.

    ``tools_list is None`` → base verbatim (preset + derived entries).
    Incremental (``+/-``) → base entries keep their origins, additions are
    declared. Wholesale (unprefixed) → the whole list is the declaration.
    Capability-contributed names classify as CAPABILITY_DERIVED and carry
    the contributing registration name, rather than appearing preset-derived.
    """
    derived_by_name = {entry.tool: entry for entry in derived}
    preset_set = set(preset_names)
    if tools_list is None:
        entries: list[ToolEntryProvenance] = []
        for name in merged_tools:
            derived_entry = derived_by_name.get(name)
            capability = capability_tool_owners.get(name)
            if derived_entry is not None:
                entries.append(derived_entry)
            elif capability is not None:
                entries.append(
                    ToolEntryProvenance(
                        tool=name,
                        origin=ToolOrigin.CAPABILITY_DERIVED,
                        capability=capability,
                    )
                )
            else:
                entries.append(ToolEntryProvenance(tool=name, origin=ToolOrigin.PRESET))
        return entries
    if any(entry.startswith(("+", "-")) for entry in tools_list):
        entries = []
        for name in merged_tools:
            derived_entry = derived_by_name.get(name)
            capability = capability_tool_owners.get(name)
            if derived_entry is not None:
                entries.append(derived_entry)
            elif capability is not None:
                entries.append(
                    ToolEntryProvenance(
                        tool=name,
                        origin=ToolOrigin.CAPABILITY_DERIVED,
                        capability=capability,
                    )
                )
            elif name in preset_set:
                entries.append(ToolEntryProvenance(tool=name, origin=ToolOrigin.PRESET))
            else:
                entries.append(ToolEntryProvenance(tool=name, origin=declared_origin))
        return entries
    return [ToolEntryProvenance(tool=name, origin=declared_origin) for name in merged_tools]


def _classify_hooks(
    merged_hooks: list[str],
    *,
    position_hooks: list[str],
    capability_owners: Mapping[str, str],
) -> list[HookEntryProvenance]:
    """Classify hook entries by origin: the position-default base wins a
    duplicated name (first-in-base, mirroring ``_classify_tools``'s preset
    precedence), then capability contributions, then the declaration."""
    position_set = set(position_hooks)
    entries: list[HookEntryProvenance] = []
    for name in merged_hooks:
        if name in position_set:
            entries.append(HookEntryProvenance(hook=name, origin=HookOrigin.POSITION_DEFAULT))
        elif name in capability_owners:
            entries.append(
                HookEntryProvenance(
                    hook=name,
                    origin=HookOrigin.CAPABILITY_DERIVED,
                    capability=capability_owners[name],
                )
            )
        else:
            entries.append(HookEntryProvenance(hook=name, origin=HookOrigin.LOCAL_HOOKS))
    return entries


def _memory_overrides(
    memory: MemoryDeclaration | None,
    *,
    is_root: bool,
) -> MemoryOverrides:
    """Project the resolved memory declaration onto the AssemblySpec face.

    Root parity with the legacy road's factory-path projection: the legacy
    main roster ALWAYS carried the memory-toggle dump (defaults False/False),
    so an undeclared root projects ``MemoryOverrides(archive_enabled=False,
    core_enabled=False)`` — not all-None. Non-roots without a declaration
    project all-None (no overrides). A declared block projects its concrete
    toggles (the typed face — the legacy raw-dict None-for-absent-keys
    behavior dies with the declaration face).
    """
    if memory is None:
        if is_root:
            return MemoryOverrides(archive_enabled=False, core_enabled=False)
        return MemoryOverrides()
    max_context_tokens = memory.session.max_context_tokens if memory.session is not None else None
    return MemoryOverrides(
        max_context_tokens=max_context_tokens,
        archive_enabled=memory.archive_enabled,
        core_enabled=memory.core_enabled,
    )
