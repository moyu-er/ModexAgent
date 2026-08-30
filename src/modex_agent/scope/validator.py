"""ScopeTreeValidator — two-phase pure validation (SPEC §7, ticket 03).

Pure functions, deterministic, zero side effects (TopologyValidator-style,
rules facing the declaration tree). Errors are result objects — every
entry returns ALL issues found (empty list = valid); boot wiring
(ticket 07/08) turns a non-empty list into a startup abort.

Two phases (SPEC §7, closed-loop revision):

- **Phase 1 — declaration shape, pre-derivation**
  (:func:`validate_declaration`): V1 acyclic, V2 connected, V3 exactly one
  root per pool tree, V4 kind hierarchy, V5 peer topology, V7 profile
  single-level references, V10 graph agent references, V11 name
  uniqueness, V12 external-agent capability exclusion.
- **Phase 2 — effective values, post-derivation**
  (:func:`validate_effective_configs`): V6 ``task`` present in the
  compiler-derived effective toolset of child-carrying agents, V9 non-root
  approval refused. Inputs are the compiler's derived configs (ticket 06);
  until it exists, hand-built fixtures drive the rule bodies.

V8 (wholesale list replacement) is documentation semantics — no runtime
rule. V10 is a phase-1 rule (SPEC §7 phase-1 table is authoritative; the
ticket-03 prose placing it in phase 2 is a known typo, SPEC errata
pending).

Input faces defined here (``ProfileDeclaration``, ``GraphAgentReference``,
``EffectiveAgentConfig``) are the wiring contract for tickets 06/07/08;
``validator.py`` never imports ``modex_graph`` (N3) — boot extracts
graph-node references into :class:`GraphAgentReference`.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from enum import StrEnum
from typing import Final, assert_never

from pydantic import BaseModel, ConfigDict

from modex_agent.scope.spec import PoolSpec, ScopeKind, ScopeSpec

TASK_TOOL_NAME: Final = "task"
"""LLM-facing name of the subagent dispatch tool (TaskDispatchTool) — the
one tool every agent with declared children must keep (V6)."""


class RuleId(StrEnum):
    """ScopeTreeValidator rule identifiers (SPEC §7). V8 excluded."""

    ACYCLIC = "V1"
    CONNECTED = "V2"
    SINGLE_ROOT = "V3"
    KIND_HIERARCHY = "V4"
    PEER_TOPOLOGY = "V5"
    TASK_TOOL_PRESENT = "V6"
    PROFILE_SINGLE_LEVEL = "V7"
    NON_ROOT_APPROVAL = "V9"
    GRAPH_AGENT_REFERENCE = "V10"
    NAME_UNIQUENESS = "V11"
    EXTERNAL_CAPABILITIES = "V12"


class ScopeValidationIssue(BaseModel):
    """One validation finding — rule-numbered, node-named, actionable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: RuleId
    node: str
    """The offending declaration element (pool/agent/profile/graph node)."""
    message: str
    """One-sentence actionable fix guidance."""


class ProfileDeclaration(BaseModel):
    """The V7 face of one loaded profile: name + optional base reference.

    The full profile system lands with ticket 06; V7 only consumes each
    profile's optional reference to another profile (which must never be
    set — SPEC §3.4 rule 1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    profile: str | None = None
    """Base profile reference — must stay ``None``: profiles build on
    framework defaults only (single-level references)."""


class GraphAgentReference(BaseModel):
    """A (pool, agent) reference declared by an agent node in a loaded
    graph spec — the V10 input face.

    ``graph``/``node`` name the declaring spec/node for error messages;
    ``pool``/``agent`` are the reference under check.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph: str
    node: str
    pool: str
    agent: str


class EffectiveAgentConfig(BaseModel):
    """The phase-2 input face: one agent's compiler-derived effective
    values. ``tools`` is the post-derivation toolset (declared roster +
    derived communication entries), LLM-facing tool names.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pool: str
    agent: str
    tools: list[str]


# ---------------------------------------------------------------------------
# Phase 1 — declaration shape (pre-derivation)
# ---------------------------------------------------------------------------


def validate_declaration(
    spec: ScopeSpec,
    *,
    profiles: Sequence[ProfileDeclaration] = (),
    graph_agent_refs: Sequence[GraphAgentReference] = (),
) -> list[ScopeValidationIssue]:
    """Validate declaration shape: V1-V5, V7, V10-V12 (SPEC §7 phase 1).

    Args:
        spec: the loaded declaration tree.
        profiles: the loaded profile store's declarations (V7 input;
            empty until ticket 06 builds the profile system).
        graph_agent_refs: (pool, agent) references extracted from loaded
            graph specs (V10 input; boot wiring lives in ticket 07/08).

    Returns:
        All issues found, in rule order (V1, V2, V3, V4, V5, V7, V10,
        V11, V12), then declaration order within each rule. Empty = valid.
    """
    pools = _pools_of(spec)
    issues: list[ScopeValidationIssue] = []
    for pool in pools:
        issues.extend(_check_acyclic(pool))
    for pool in pools:
        issues.extend(_check_connected(pool))
    for pool in pools:
        issues.extend(_check_single_root(pool))
    issues.extend(_check_kind_hierarchy(spec))
    issues.extend(_check_peer_topology(spec))
    issues.extend(_check_profile_single_level(profiles))
    pools_by_name: dict[str, PoolSpec] = {pool.name: pool for pool in pools}
    issues.extend(_check_graph_agent_refs(pools_by_name, graph_agent_refs))
    issues.extend(_check_name_uniqueness(spec, pools))
    issues.extend(_check_external_capability_declarations(pools))
    return issues


# ---------------------------------------------------------------------------
# Phase 2 — effective values (post-derivation)
# ---------------------------------------------------------------------------


def validate_effective_configs(
    spec: ScopeSpec,
    effective_configs: Sequence[EffectiveAgentConfig],
) -> list[ScopeValidationIssue]:
    """Validate effective values: V6, V9 (SPEC §7 phase 2).

    Args:
        spec: the (phase-1 validated) declaration tree.
        effective_configs: compiler-derived per-agent effective configs —
            one entry per declared agent (ticket 06 output; hand-built
            fixtures until then).

    Returns:
        All issues found (V6 then V9, declaration order within each).
        Empty = valid.
    """
    pools = _pools_of(spec)
    toolsets: dict[tuple[str, str], list[str]] = {
        (config.pool, config.agent): list(config.tools)
        for config in effective_configs
    }
    issues = _check_task_tool_present(pools, toolsets)
    issues.extend(_check_non_root_approval(pools))
    return issues


# ---------------------------------------------------------------------------
# Rule bodies
# ---------------------------------------------------------------------------


def _pools_of(spec: ScopeSpec) -> list[PoolSpec]:
    """The declared pools of either root form (workspace-hosted or
    pool-as-root). Empty when the form is inconsistent (V4 reports)."""
    match spec.kind:
        case ScopeKind.WORKSPACE:
            if spec.workspace is not None:
                return list(spec.workspace.pools)
            return []
        case ScopeKind.POOL:
            if spec.pool is not None:
                return [spec.pool]
            return []
        case unreachable:
            assert_never(unreachable)


def _check_acyclic(pool: PoolSpec) -> list[ScopeValidationIssue]:
    """V1 — parent references form no cycle (one issue per cycle)."""
    by_name = {agent.name: agent for agent in pool.agents}
    issues: list[ScopeValidationIssue] = []
    state: dict[str, str] = {}  # name -> "open" (on current chain) | "done"
    for agent in pool.agents:
        if state.get(agent.name) is not None:
            continue
        chain: list[str] = []
        current: str | None = agent.name
        while current is not None and current not in state:
            state[current] = "open"
            chain.append(current)
            current = by_name[current].parent
        if current is not None and state.get(current) == "open":
            cycle = chain[chain.index(current) :] + [current]
            issues.append(
                ScopeValidationIssue(
                    rule=RuleId.ACYCLIC,
                    node=cycle[0],
                    message=(
                        f"pool {pool.name!r}: agent {cycle[0]!r} participates "
                        f"in the parent cycle {' → '.join(cycle)} — break the "
                        f"cycle by removing or retargeting one parent "
                        f"reference (V1)"
                    ),
                )
            )
        for name in chain:
            state[name] = "done"
    return issues


def _check_connected(pool: PoolSpec) -> list[ScopeValidationIssue]:
    """V2 — every declared node is reachable from a root agent.

    Skipped when the pool has no root: there is nothing to reach from and
    V1/V3 already carry the failure.
    """
    roots = [agent.name for agent in pool.agents if agent.parent is None]
    if not roots:
        return []
    children: dict[str, list[str]] = {}
    for agent in pool.agents:
        if agent.parent is not None:
            children.setdefault(agent.parent, []).append(agent.name)
    reachable: set[str] = set(roots)
    queue: deque[str] = deque(roots)
    while queue:
        for child in children.get(queue.popleft(), []):
            if child not in reachable:
                reachable.add(child)
                queue.append(child)
    return [
        ScopeValidationIssue(
            rule=RuleId.CONNECTED,
            node=agent.name,
            message=(
                f"pool {pool.name!r}: agent {agent.name!r} is not reachable "
                f"from a root agent — its parent chain must lead to the "
                f"tree root; fix or remove the parent reference (V2)"
            ),
        )
        for agent in pool.agents
        if agent.name not in reachable
    ]


def _check_single_root(pool: PoolSpec) -> list[ScopeValidationIssue]:
    """V3 — exactly one in-degree-0 node per pool tree."""
    roots = [agent.name for agent in pool.agents if agent.parent is None]
    if len(roots) == 1:
        return []
    if not roots:
        return [
            ScopeValidationIssue(
                rule=RuleId.SINGLE_ROOT,
                node=pool.name,
                message=(
                    f"pool {pool.name!r} declares no root agent — exactly "
                    f"one agent must have no parent; add a root agent or "
                    f"fix the parent references (V3)"
                ),
            )
        ]
    names = ", ".join(repr(name) for name in roots)
    return [
        ScopeValidationIssue(
            rule=RuleId.SINGLE_ROOT,
            node=pool.name,
            message=(
                f"pool {pool.name!r} declares {len(roots)} root agents "
                f"({names}) — exactly one agent may have no parent; give "
                f"the others a parent (V3)"
            ),
        )
    ]


def _check_kind_hierarchy(spec: ScopeSpec) -> list[ScopeValidationIssue]:
    """V4 — kind matches the declared layers: workspace > pool; agents are
    pool-internal data, nesting depth unlimited.

    ``ScopeSpec``'s own validator already enforces the pairing at
    construction; this re-check keeps the validator self-contained for
    bypassed input (TopologyValidator precedent).
    """
    form_ok = False
    match spec.kind:
        case ScopeKind.WORKSPACE:
            form_ok = spec.workspace is not None and spec.pool is None
        case ScopeKind.POOL:
            form_ok = spec.pool is not None and spec.workspace is None
        case unreachable:
            assert_never(unreachable)
    if form_ok:
        return []
    return [
        ScopeValidationIssue(
            rule=RuleId.KIND_HIERARCHY,
            node=spec.kind.value,
            message=(
                f"kind {spec.kind.value!r} does not match the declared "
                f"layers — a workspace declaration carries exactly the "
                f"workspace layer hosting pools, a pool declaration exactly "
                f"the pool layer, and agents are pool-internal data (V4)"
            ),
        )
    ]


def _check_peer_topology(spec: ScopeSpec) -> list[ScopeValidationIssue]:
    """V5 — peer endpoints exist, same workspace (v1), bidirectional
    (ADR-0019); pool-as-root declarations cannot declare peers.

    Root-to-root is structural: peers reference POOLS and V3 guarantees
    each pool a unique root — no agent-level peer syntax exists.
    """
    issues: list[ScopeValidationIssue] = []
    if spec.kind is ScopeKind.POOL:
        pool = spec.pool
        if pool is not None and pool.peers:
            issues.append(
                ScopeValidationIssue(
                    rule=RuleId.PEER_TOPOLOGY,
                    node=pool.name,
                    message=(
                        f"pool {pool.name!r} is declared as the root scope "
                        f"(no workspace layer) and cannot declare peers in "
                        f"v1 — the same-workspace premise is undefined for "
                        f"pool-as-root declarations; host the pool in a "
                        f"workspace to use peer links (V5)"
                    ),
                )
            )
        return issues
    workspace = spec.workspace
    if workspace is None:
        return issues
    by_name = {pool.name: pool for pool in workspace.pools}
    for pool in workspace.pools:
        for peer in pool.peers:
            target = by_name.get(peer)
            if target is None:
                issues.append(
                    ScopeValidationIssue(
                        rule=RuleId.PEER_TOPOLOGY,
                        node=pool.name,
                        message=(
                            f"pool {pool.name!r} declares peer {peer!r} "
                            f"which is not declared in workspace "
                            f"{workspace.name!r} — v1 peer links are "
                            f"same-workspace only (N5); declare pool "
                            f"{peer!r} in this workspace or remove the "
                            f"link (V5)"
                        ),
                    )
                )
            elif pool.name not in target.peers:
                issues.append(
                    ScopeValidationIssue(
                        rule=RuleId.PEER_TOPOLOGY,
                        node=target.name,
                        message=(
                            f"peer link {pool.name!r}→{target.name!r} is not "
                            f"bidirectional — pool {target.name!r} must "
                            f"list {pool.name!r} in its peers (ADR-0019) (V5)"
                        ),
                    )
                )
    return issues


def _check_profile_single_level(
    profiles: Sequence[ProfileDeclaration],
) -> list[ScopeValidationIssue]:
    """V7 — profiles may not reference other profiles (SPEC §3.4 rule 1)."""
    return [
        ScopeValidationIssue(
            rule=RuleId.PROFILE_SINGLE_LEVEL,
            node=profile.name,
            message=(
                f"profile {profile.name!r} references base profile "
                f"{profile.profile!r} — profiles may build on framework "
                f"defaults only (single-level references, SPEC §3.4 rule "
                f"1); inline the referenced overrides instead (V7)"
            ),
        )
        for profile in profiles
        if profile.profile is not None
    ]


def _check_graph_agent_refs(
    pools_by_name: dict[str, PoolSpec],
    refs: Sequence[GraphAgentReference],
) -> list[ScopeValidationIssue]:
    """V10 — every graph spec agent-node (pool, agent) reference exists in
    the declaration tree (typo'd names fail at boot, not at runtime)."""
    agent_names: dict[str, set[str]] = {
        name: {agent.name for agent in pool.agents}
        for name, pool in pools_by_name.items()
    }
    issues: list[ScopeValidationIssue] = []
    for ref in refs:
        if ref.pool not in pools_by_name:
            issues.append(
                ScopeValidationIssue(
                    rule=RuleId.GRAPH_AGENT_REFERENCE,
                    node=ref.node,
                    message=(
                        f"graph {ref.graph!r} node {ref.node!r} references "
                        f"pool {ref.pool!r} which is not declared — fix the "
                        f"pool name or declare the pool (V10)"
                    ),
                )
            )
        elif ref.agent not in agent_names[ref.pool]:
            issues.append(
                ScopeValidationIssue(
                    rule=RuleId.GRAPH_AGENT_REFERENCE,
                    node=ref.node,
                    message=(
                        f"graph {ref.graph!r} node {ref.node!r} references "
                        f"agent {ref.agent!r} which is not declared in pool "
                        f"{ref.pool!r} — fix the agent name; typo'd names "
                        f"fail here at boot instead of at runtime (V10)"
                    ),
                )
            )
    return issues


def _check_name_uniqueness(
    spec: ScopeSpec,
    pools: Sequence[PoolSpec],
) -> list[ScopeValidationIssue]:
    """V11 — pool-internal agent names and workspace-internal pool names
    are unique (the keyed lookup chain rides on this)."""
    issues: list[ScopeValidationIssue] = []
    if spec.workspace is not None:
        pool_counts: dict[str, int] = {}
        for pool in spec.workspace.pools:
            pool_counts[pool.name] = pool_counts.get(pool.name, 0) + 1
        for name, count in pool_counts.items():
            if count > 1:
                issues.append(
                    ScopeValidationIssue(
                        rule=RuleId.NAME_UNIQUENESS,
                        node=name,
                        message=(
                            f"pool name {name!r} is declared {count} times "
                            f"in workspace {spec.workspace.name!r} — "
                            f"workspace-internal pool names must be unique; "
                            f"duplicate declarations silently collide (V11)"
                        ),
                    )
                )
    for pool in pools:
        agent_counts: dict[str, int] = {}
        for agent in pool.agents:
            agent_counts[agent.name] = agent_counts.get(agent.name, 0) + 1
        for name, count in agent_counts.items():
            if count > 1:
                issues.append(
                    ScopeValidationIssue(
                        rule=RuleId.NAME_UNIQUENESS,
                        node=name,
                        message=(
                            f"agent name {name!r} is declared {count} times "
                            f"in pool {pool.name!r} — pool-internal agent "
                            f"names must be unique; name collisions silently "
                            f"route to the last declaration (V11)"
                        ),
                    )
                )
    return issues


def _check_external_capability_declarations(
    pools: Sequence[PoolSpec],
) -> list[ScopeValidationIssue]:
    """V12 — external agents cannot declare native capability overrides."""
    return [
        ScopeValidationIssue(
            rule=RuleId.EXTERNAL_CAPABILITIES,
            node=agent.name,
            message=(
                f"pool {pool.name!r}: external agent {agent.name!r} declares "
                "capabilities — explicit capability declarations are invalid "
                "for external agents because external agents take no native "
                "component face; remove the capabilities block (V12)"
            ),
        )
        for pool in pools
        for agent in pool.agents
        if agent.provider_kind is not None and agent.capabilities
    ]


# ---------------------------------------------------------------------------
# Phase-2 rule bodies
# ---------------------------------------------------------------------------


def _check_task_tool_present(
    pools: Sequence[PoolSpec],
    toolsets: dict[tuple[str, str], list[str]],
) -> list[ScopeValidationIssue]:
    """V6 — agents with declared children keep ``task`` in their derived
    effective toolset (prevents silently orphaned subtrees)."""
    issues: list[ScopeValidationIssue] = []
    for pool in pools:
        parents_with_children = {
            agent.parent for agent in pool.agents if agent.parent is not None
        }
        for agent in pool.agents:
            if agent.name not in parents_with_children:
                continue
            tools = toolsets.get((pool.name, agent.name))
            if tools is None:
                issues.append(
                    ScopeValidationIssue(
                        rule=RuleId.TASK_TOOL_PRESENT,
                        node=agent.name,
                        message=(
                            f"pool {pool.name!r}: agent {agent.name!r} has "
                            f"declared children but no effective toolset was "
                            f"provided — the phase-2 input must cover every "
                            f"declared agent (compiler output) (V6)"
                        ),
                    )
                )
            elif TASK_TOOL_NAME not in tools:
                issues.append(
                    ScopeValidationIssue(
                        rule=RuleId.TASK_TOOL_PRESENT,
                        node=agent.name,
                        message=(
                            f"pool {pool.name!r}: agent {agent.name!r} has "
                            f"declared children but its effective toolset "
                            f"{tools!r} drops the {TASK_TOOL_NAME!r} tool — "
                            f"the subtree would be silently unreachable; "
                            f"keep {TASK_TOOL_NAME!r} in the tools list or "
                            f"remove the children (V6)"
                        ),
                    )
                )
    return issues


def _check_non_root_approval(
    pools: Sequence[PoolSpec],
) -> list[ScopeValidationIssue]:
    """V9 — non-root agents may not declare approval (ADR-0008 main-only;
    the unified AgentSpec no longer blocks approval sinking structurally)."""
    issues: list[ScopeValidationIssue] = []
    for pool in pools:
        for agent in pool.agents:
            if agent.parent is not None and agent.approval is not None:
                issues.append(
                    ScopeValidationIssue(
                        rule=RuleId.NON_ROOT_APPROVAL,
                        node=agent.name,
                        message=(
                            f"pool {pool.name!r}: non-root agent "
                            f"{agent.name!r} declares approval — approval "
                            f"is root-only (ADR-0008); a non-root turn "
                            f"would hang waiting for an approval that "
                            f"never arrives. Remove the approval block (V9)"
                        ),
                    )
                )
    return issues
