"""Pure pre-compile overlays for loaded scope declarations.

Apply :func:`apply_scope_overlay` after ``load_scope_declaration`` and before
``boot_scope_spec``. The transform performs no validation shortcut: boot still
runs declaration rules V1-V11 and effective-value validation over the adjusted
tree, and the resulting declaration must satisfy them all.

``tools`` overlays do not merge — entries are APPENDED to the declared roster
(verbatim when the declaration has none); the compiler's single
``_merge_tools`` pass owns all merging over the full base (preset, derived,
and supplement-derived names). One combination is rejected loudly: prefixed
``+/-`` overlay entries against a wholesale (all-unprefixed) declared roster
raise ``ValueError`` — the concatenation would silently flip wholesale
semantics to incremental and reintroduce the preset tools.
"""

from __future__ import annotations

from typing import Any, assert_never

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.scope.profile import merge_memory_declarations
from modex_agent.scope.spec import (
    AgentSpec,
    MemoryDeclaration,
    PoolSpec,
    ScopeKind,
    ScopeSpec,
)
from modex_agent.tools.presets import ToolPreset


class AgentOverlay(BaseModel):
    """Optional per-field changes to one declared agent.

    ``tools`` entries are APPENDED to the declaration's roster — or become
    the roster verbatim when the declaration has none; the overlay never
    merges, the compiler's single ``_merge_tools`` pass does, over the full
    base (preset, derived, and supplement-derived names). Consequences: a
    ``-name`` overlay entry removes a ``+name`` declared entry (the compiler
    matches the stripped name against its full base); unprefixed overlay
    entries appended to a prefixed declared list act as baseline annotations
    (ignored by the mixed-list rule); unprefixed overlay entries appended to
    an unprefixed declared roster concatenate — duplicates are possible and
    visible in the compiled roster (no current consumer constructs
    unprefixed overlay entries; all repo constructions are minus-only).
    Prefixed ``+/-`` entries against a wholesale (all-unprefixed) declared
    roster are REJECTED with ``ValueError``: the concatenation would silently
    flip wholesale semantics to incremental and reintroduce the preset tools.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    toolset: ToolPreset | None = None
    tools: list[str] | None = None
    memory: MemoryDeclaration | None = None
    system_prompt_provider: str | None = None
    # Open heterogeneous payload mirroring AgentSpec; the named prompt factory
    # validates its config model during assembly.
    system_prompt_provider_config: dict[str, Any] = Field(default_factory=dict)
    strip_approval: bool = False
    # Host-independent tool rosters: trial containers ship no MCP registry, so
    # harnesses declare the absence instead of inheriting the host's.
    strip_mcp: bool = False


class PoolOverlay(BaseModel):
    """Optional roster and agent changes for one named pool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    keep_agents: list[str] | None = None
    agents: dict[str, AgentOverlay] = Field(default_factory=dict)


class ScopeOverlay(BaseModel):
    """Closed overlay schema for one loaded scope declaration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strip_peers: bool = False
    pools: dict[str, PoolOverlay] = Field(default_factory=dict)


def apply_scope_overlay(spec: ScopeSpec, overlay: ScopeOverlay) -> ScopeSpec:
    """Return a frozen copy of ``spec`` with ``overlay`` applied."""
    pools = _pools_of(spec)
    known_pools = {pool.name for pool in pools}
    unknown_pools = set(overlay.pools) - known_pools
    if unknown_pools:
        names = sorted(unknown_pools)
        msg = f"scope overlay references unknown pool(s): {names}"
        raise ValueError(msg)

    adjusted = [
        _apply_pool_overlay(
            pool,
            overlay.pools.get(pool.name),
            strip_peers=overlay.strip_peers,
        )
        for pool in pools
    ]
    match spec.kind:
        case ScopeKind.WORKSPACE:
            if spec.workspace is None:
                msg = "workspace scope has no workspace layer"
                raise ValueError(msg)
            workspace = spec.workspace.model_copy(update={"pools": adjusted})
            return spec.model_copy(update={"workspace": workspace})
        case ScopeKind.POOL:
            if len(adjusted) != 1:
                msg = "pool-root scope must contain exactly one pool"
                raise ValueError(msg)
            return spec.model_copy(update={"pool": adjusted[0]})
        case unreachable:
            assert_never(unreachable)


def _pools_of(spec: ScopeSpec) -> list[PoolSpec]:
    match spec.kind:
        case ScopeKind.WORKSPACE:
            if spec.workspace is None:
                msg = "workspace scope has no workspace layer"
                raise ValueError(msg)
            return list(spec.workspace.pools)
        case ScopeKind.POOL:
            if spec.pool is None:
                msg = "pool-root scope has no pool layer"
                raise ValueError(msg)
            return [spec.pool]
        case unreachable:
            assert_never(unreachable)


def _apply_pool_overlay(
    pool: PoolSpec,
    overlay: PoolOverlay | None,
    *,
    strip_peers: bool,
) -> PoolSpec:
    updates: dict[str, list[str] | list[AgentSpec]] = {}
    if strip_peers:
        updates["peers"] = []
    if overlay is None:
        return pool.model_copy(update=updates)

    known_agents = {agent.name for agent in pool.agents}
    referenced_agents = set(overlay.agents)
    if overlay.keep_agents is not None:
        referenced_agents.update(overlay.keep_agents)
    unknown_agents = referenced_agents - known_agents
    if unknown_agents:
        names = sorted(unknown_agents)
        msg = f"scope overlay for pool {pool.name!r} references unknown agent(s): {names}"
        raise ValueError(msg)

    agents = list(pool.agents)
    if overlay.keep_agents is not None:
        root = pool.root_agent
        if root.name not in overlay.keep_agents:
            msg = (
                f"scope overlay for pool {pool.name!r} cannot drop root agent "
                f"{root.name!r}"
            )
            raise ValueError(msg)
        keep = set(overlay.keep_agents)
        agents = [agent for agent in agents if agent.name in keep]

    updates["agents"] = [
        _apply_agent_overlay(agent, overlay.agents.get(agent.name))
        for agent in agents
    ]
    return pool.model_copy(update=updates)


def _apply_agent_overlay(
    agent: AgentSpec,
    overlay: AgentOverlay | None,
) -> AgentSpec:
    if overlay is None:
        return agent

    updates: dict[str, ToolPreset | list[str] | MemoryDeclaration | str | dict[str, Any] | None] = {}
    if overlay.toolset is not None:
        updates["toolset"] = overlay.toolset
    if overlay.tools is not None:
        # Wholesale (all-unprefixed) declared roster + prefixed overlay
        # entries would concatenate into a MIXED list: the compiler's merge
        # then flips wholesale semantics to incremental and silently
        # reintroduces the preset tools. Reject loudly.
        if (
            agent.tools is not None
            and any(entry.startswith(("+", "-")) for entry in overlay.tools)
            and not any(entry.startswith(("+", "-")) for entry in agent.tools)
        ):
            msg = (
                f"scope overlay for agent {agent.name!r}: prefixed +/- tools "
                "entries cannot apply to a wholesale (unprefixed) declared "
                "tools list — the concatenation would silently flip "
                "wholesale semantics to incremental and reintroduce the "
                "preset tools; declare the roster with +/- entries instead, "
                "or use an unprefixed overlay list"
            )
            raise ValueError(msg)
        updates["tools"] = (
            list(overlay.tools)
            if agent.tools is None
            else [*agent.tools, *overlay.tools]
        )
    if overlay.memory is not None:
        updates["memory"] = merge_memory_declarations(agent.memory, overlay.memory)
    if overlay.system_prompt_provider is not None:
        updates["system_prompt_provider"] = overlay.system_prompt_provider
    if "system_prompt_provider_config" in overlay.model_fields_set:
        updates["system_prompt_provider_config"] = dict(
            overlay.system_prompt_provider_config
        )
    if overlay.strip_approval:
        updates["approval"] = None
    if overlay.strip_mcp:
        updates["mcp"] = []
    return agent.model_copy(update=updates)
