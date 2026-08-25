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
  origin, and the O3 same-name replacement records (``edit ← aci``).

Derivation table (SPEC §5.2 — position/tree-shape derived, never roster
declared, never materialize-time side-registered; the TOOL-slot FW
factories resolving these entries and the per-agent
``CommunicationTargetStore`` wiring land with tickets 07/12 — the entry
names are the contract):

- ``task`` — agents with declared children (root or not); targets are the
  DIRECT children only (grandchildren belong to the child's own task).
- ``send_to_agent`` — every non-root node; target is the parent.
- ``send_to_peer`` — roots of pools with links; targets are the peer pool
  names.

An explicitly declared unprefixed ``tools`` list — local or profile — is
wholesale (O4/V8): it REPLACES the preset-derived base including the
derived entries, and V6 catches child-carrying agents that drop ``task``.
Incremental ``+/-`` lists merge over the base (derived entries included,
so ``-task`` is expressible and V6-guarded).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.multi_agent.execution_strategy import strategy_name_of
from modex_agent.plugins.assembly.spec import AssemblySpec, MemoryOverrides
from modex_agent.scope.defaults import (
    PositionDefaults,
    defaults_for_position,
    effective_defaults,
)
from modex_agent.scope.derivation import (
    _DEFAULT_LLM_PROVIDER,
    _derive_agent_type,
    _expand_preset_tool_names,
    _expand_system_prompt,
    _merge_hooks,
    _merge_tools,
)
from modex_agent.scope.profile import (
    STANDARD_PROFILES,
    Profile,
    ProfileStore,
    merge_memory_declarations,
)
from modex_agent.scope.spec import AgentSpec, MemoryDeclaration, PoolSpec, ScopeSpec
from modex_agent.scope.validator import TASK_TOOL_NAME, EffectiveAgentConfig, _pools_of
from modex_agent.tools.presets import ToolSupplement, get_supplement_tool_names
from modex_agent.workspace.context import WorkspaceContext

SEND_TO_AGENT_TOOL_NAME: Final = "send_to_agent"
"""Subagent→parent consultation tool (``SendToAgentTool`` in
``multi_agent/tools.py``) — the derived entry name; the TOOL-slot factory
wiring lands with ticket 12."""

SEND_TO_PEER_TOOL_NAME: Final = "send_to_peer"
"""Root peer-messaging tool (cf. ``SEND_TO_PEER_TOOL_NAME`` in
``multi_agent/tools.py``) — the derived entry name; the factory and
bus/tree resolution wiring land with tickets 07/13. Redeclared here to
keep the scope package's import graph light."""

_ACI_DEFAULT_TOOL: Final = "edit"
_ACI_REPLACEMENT_TOOL: Final = "aci_edit"


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
    """``tool_supplements`` entry (including O3 replacements)."""
    DERIVED_TASK = "derived_task"
    DERIVED_SEND_TO_AGENT = "derived_send_to_agent"
    DERIVED_SEND_TO_PEER = "derived_send_to_peer"


class FieldProvenance(BaseModel):
    """One field's winning layer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    """AgentSpec field name whose effective value this records."""
    layer: ProvenanceLayer
    profile: str | None = None
    """Bound profile name when ``layer`` is PROFILE."""


class ToolEntryProvenance(BaseModel):
    """One effective tool entry's origin."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    origin: ToolOrigin
    replaces: str | None = None
    """O3: the default entry this supplement entry replaced (``edit``)."""
    targets: list[str] = Field(default_factory=list)
    """Derived communication entries only: task → direct child agents;
    send_to_agent → ``(parent,)``; send_to_peer → peer pool names."""


class ToolReplacement(BaseModel):
    """One O3 same-name replacement record (SPEC §3.5): a supplement's
    product replaced a default tool entry in the effective toolset
    (``edit ← aci_edit``). Per-pool granularity — the roster/supplement
    references decide, unlike O2's global registry priority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_tool: str
    replacement_tool: str
    supplement: ToolSupplement


class AgentProvenance(BaseModel):
    """One agent's bill data (WebUI ticket 16): per-field layers, per-tool
    origins, and the O3 replacement records."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pool: str
    agent: str
    fields: list[FieldProvenance]
    tools: list[ToolEntryProvenance]
    replacements: list[ToolReplacement]

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
) -> ScopeCompilation:
    """Compile a validated declaration tree into per-agent artifacts.

    Args:
        spec: the (phase-1 validated) declaration tree.
        workspace_ctx: runtime object threaded into every AssemblySpec
            (excluded from byte-stability comparisons).
        profiles: the profile store supplying the profile layer.
        default_llm_provider: fallback LLM provider component name (the
            ``default`` factory).

    Returns:
        One :class:`CompiledAgent` per declared agent, in declaration
        order. Raises ``ValueError`` for a pool without exactly one root —
        the compiler requires a validated tree (V3).
    """
    compiled: list[CompiledAgent] = []
    for pool in _pools_of(spec):
        compiled.extend(
            _compile_pool(
                pool,
                profiles=profiles,
                workspace_ctx=workspace_ctx,
                default_llm_provider=default_llm_provider,
            )
        )
    return ScopeCompilation(agents=compiled)


def _compile_pool(
    pool: PoolSpec,
    *,
    profiles: ProfileStore,
    workspace_ctx: WorkspaceContext,
    default_llm_provider: str,
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
        ProvenanceLayer.LOCAL
        if agent.toolset is not None
        else ProvenanceLayer.FRAMEWORK
    )
    bound = profiles.get(toolset.value)
    profile_tools = bound.tools if bound is not None else None
    profile_supplements = bound.tool_supplements if bound is not None else None
    profile_eager = bound.eager if bound is not None else None
    profile_max_steps = bound.max_steps if bound is not None else None
    profile_memory = bound.memory if bound is not None else None

    tools_list, tools_layer = _layered(agent.tools, profile_tools)
    supplements, supplements_layer = _layered(
        list(agent.tool_supplements)
        if "tool_supplements" in local_fields
        else None,
        profile_supplements,
    )
    if supplements is None:
        supplements = []
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
        agent.model_copy(
            update={"toolset": toolset, "eager": eager, "memory": merged_memory}
        )
    )

    # ── tools pipeline: preset expansion + derived entries + O3 ──────────
    derived = _derived_entries(agent, pool=pool, children=children)
    preset_names = _expand_preset_tool_names(toolset)
    base = preset_names + [entry.tool for entry in derived]
    merged_tools = _merge_tools(base, tools_list)
    final_tools, supplement_entries, replacements = _apply_supplements(
        merged_tools, supplements
    )
    declared_origin = (
        ToolOrigin.LOCAL_TOOLS if agent.tools is not None else ToolOrigin.PROFILE_TOOLS
    )
    tool_provenance = _classify_tools(
        merged_tools,
        derived=derived,
        preset_names=preset_names,
        tools_list=tools_list,
        declared_origin=declared_origin,
    )
    tool_provenance.extend(supplement_entries)

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
        hooks=_merge_hooks(agent.hooks),
        hook_configs=dict(agent.hook_configs or {}),
        llm_provider=agent.llm_provider or default_llm_provider,
        llm_provider_config=dict(agent.llm_provider_config or {}),
        system_prompt_provider=system_prompt_provider,
        system_prompt_config=system_prompt_config,
        memory_overrides=_memory_overrides(merged_memory, is_root=is_root),
        memory_system=agent.memory_system,
        memory_system_config=dict(agent.memory_system_config),
        execution_strategy=strategy_name_of(agent.execution_strategy),
        provider_kind=(
            agent.provider_kind.value if agent.provider_kind is not None else None
        ),
        mcp_servers=list(agent.mcp),
        interceptors=list(agent.interceptors or []) if is_root else [],
        interceptor_configs=dict(agent.interceptor_configs or {}) if is_root else {},
        commands=list(agent.commands) if is_root and agent.commands is not None else None,
        workspace_ctx=workspace_ctx,
    )
    return CompiledAgent(
        spec=spec,
        effective=EffectiveAgentConfig(
            pool=pool.name, agent=agent.name, tools=list(final_tools)
        ),
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
                FieldProvenance(
                    field="tool_supplements",
                    layer=supplements_layer,
                    profile=_profile_name(bound, supplements_layer),
                ),
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
            ],
            tools=tool_provenance,
            replacements=replacements,
        ),
    )


# ─── Internal helpers ──────────────────────────────────────────────────────


def _layered[T](
    local: T | None, profile_value: T | None
) -> tuple[T | None, ProvenanceLayer]:
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


def _derived_entries(
    agent: AgentSpec,
    *,
    pool: PoolSpec,
    children: dict[str, list[str]],
) -> list[ToolEntryProvenance]:
    """The §5.2 derived communication entries for one agent, in table
    order (task, send_to_agent, send_to_peer)."""
    entries: list[ToolEntryProvenance] = []
    if agent.name in children:
        entries.append(
            ToolEntryProvenance(
                tool=TASK_TOOL_NAME,
                origin=ToolOrigin.DERIVED_TASK,
                targets=list(children[agent.name]),
            )
        )
    if agent.parent is not None:
        entries.append(
            ToolEntryProvenance(
                tool=SEND_TO_AGENT_TOOL_NAME,
                origin=ToolOrigin.DERIVED_SEND_TO_AGENT,
                targets=[agent.parent],
            )
        )
    if agent.parent is None and pool.peers:
        entries.append(
            ToolEntryProvenance(
                tool=SEND_TO_PEER_TOOL_NAME,
                origin=ToolOrigin.DERIVED_SEND_TO_PEER,
                targets=list(pool.peers),
            )
        )
    return entries


def _apply_supplements(
    tools: list[str],
    supplements: list[ToolSupplement],
) -> tuple[list[str], list[ToolEntryProvenance], list[ToolReplacement]]:
    """Apply tool supplements with O3 same-name replacement accounting.

    ACI is a drop-in UPGRADE — the ``aci_edit`` factory yields
    a tool whose LLM-facing name is still ``edit``, so the default
    ``edit`` entry is removed and the replacement recorded. Other
    supplements append their produced tool names.
    """
    result = list(tools)
    entries: list[ToolEntryProvenance] = []
    replacements: list[ToolReplacement] = []
    for supplement in supplements:
        if supplement is ToolSupplement.ACI:
            had_default = _ACI_DEFAULT_TOOL in result
            result = [name for name in result if name != _ACI_DEFAULT_TOOL]
            if _ACI_REPLACEMENT_TOOL not in result:
                result.append(_ACI_REPLACEMENT_TOOL)
            if had_default:
                replacements.append(
                    ToolReplacement(
                        default_tool=_ACI_DEFAULT_TOOL,
                        replacement_tool=_ACI_REPLACEMENT_TOOL,
                        supplement=supplement,
                    )
                )
            entries.append(
                ToolEntryProvenance(
                    tool=_ACI_REPLACEMENT_TOOL,
                    origin=ToolOrigin.SUPPLEMENT,
                    replaces=_ACI_DEFAULT_TOOL if had_default else None,
                )
            )
            continue
        for name in get_supplement_tool_names([supplement]):
            if name not in result:
                result.append(name)
                entries.append(
                    ToolEntryProvenance(tool=name, origin=ToolOrigin.SUPPLEMENT)
                )
    return result, entries, replacements


def _classify_tools(
    merged_tools: list[str],
    *,
    derived: list[ToolEntryProvenance],
    preset_names: list[str],
    tools_list: list[str] | None,
    declared_origin: ToolOrigin,
) -> list[ToolEntryProvenance]:
    """Classify the pre-supplement tool entries by origin.

    ``tools_list is None`` → base verbatim (preset + derived entries).
    Incremental (``+/-``) → base entries keep their origins, additions are
    declared. Wholesale (unprefixed) → the whole list is the declaration.
    """
    derived_by_name = {entry.tool: entry for entry in derived}
    preset_set = set(preset_names)
    if tools_list is None:
        entries: list[ToolEntryProvenance] = []
        for name in merged_tools:
            derived_entry = derived_by_name.get(name)
            if derived_entry is not None:
                entries.append(derived_entry)
            else:
                entries.append(ToolEntryProvenance(tool=name, origin=ToolOrigin.PRESET))
        return entries
    if any(entry.startswith(("+", "-")) for entry in tools_list):
        entries = []
        for name in merged_tools:
            derived_entry = derived_by_name.get(name)
            if derived_entry is not None:
                entries.append(derived_entry)
            elif name in preset_set:
                entries.append(ToolEntryProvenance(tool=name, origin=ToolOrigin.PRESET))
            else:
                entries.append(ToolEntryProvenance(tool=name, origin=declared_origin))
        return entries
    return [
        ToolEntryProvenance(tool=name, origin=declared_origin)
        for name in merged_tools
    ]


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
    max_context_tokens = (
        memory.session.max_context_tokens if memory.session is not None else None
    )
    return MemoryOverrides(
        max_context_tokens=max_context_tokens,
        archive_enabled=memory.archive_enabled,
        core_enabled=memory.core_enabled,
    )
