"""Workspace peer-link resolution (SPEC §5.2, ticket 13 — SG1 closure).

The FW-ized Phase-2 peer wiring: at workspace materialize time, after
every pool of the bundle is built, each pool's declared peer links
resolve through the workspace's OWN resource bundle — the peer pool's
tree reference is read from the peer's :class:`PoolInstance` in the same
bundle (the v1 same-workspace invariant, SPEC §5.3: both endpoints evict
with the bundle as one unit, so a dangling cross-workspace reference is
not constructible), and the peer NORMAL target joins the root's
per-agent :class:`CommunicationTargetStore`. The derived
``send_to_peer`` TOOL factory (ticket 07) reads the store — unchanged.

Links arrive over the scope path: :func:`peer_links_from_declaration`
extracts them from a loaded declaration — the single link source since
ticket 11 deleted the legacy road.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from modex_agent.core.agent import AgentCommKind, ExecutionStrategyKind
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.multi_agent.tools import CommunicationTarget
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeSpec


class PeerLink(BaseModel):
    """One declared peer link's endpoint face (the resolution input).

    Carries the link's declaration-side facts; the runtime facts (the
    peer root's booted name, its tree reference) resolve from the
    resource bundle at wiring time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    peer_pool: str
    peer_agent: str
    """The peer root agent's DECLARED name (a link-face declaration fact —
    static consumers like the env-spec agent-pool map read it; the booted
    runtime facts still resolve from the bundle)."""
    peer_description: str = ""
    peer_execution_strategy: ExecutionStrategyKind = ExecutionStrategyKind.REACT


def peer_links_from_declaration(spec: ScopeSpec) -> dict[str, tuple[PeerLink, ...]]:
    """Extract each pool's peer links from a loaded declaration (scope path).

    Only the workspace form carries links (V5 refuses pool-as-root
    peers); each link's face is the peer pool's root agent declaration.
    The result covers only pools that declare peers, in declaration
    order; the peers of each pool keep their declared order.
    """
    if spec.workspace is None:
        return {}
    pools_by_name = {pool.name: pool for pool in spec.workspace.pools}
    links: dict[str, tuple[PeerLink, ...]] = {}
    for pool in spec.workspace.pools:
        if not pool.peers:
            continue
        pool_links: list[PeerLink] = []
        for peer_name in pool.peers:
            peer_root = _root_agent_of(pools_by_name, peer_name)
            pool_links.append(
                PeerLink(
                    peer_pool=peer_name,
                    peer_agent=peer_root.name,
                    peer_description=peer_root.description,
                    peer_execution_strategy=ExecutionStrategyKind(peer_root.execution_strategy),
                )
            )
        links[pool.name] = tuple(pool_links)
    return links


def resolve_peer_targets(
    pools: Mapping[str, PoolInstance],
    links: Mapping[str, Sequence[PeerLink]],
) -> None:
    """Resolve the workspace's peer links into per-agent store entries.

    For each pool with links, every link resolves its tree reference
    from the peer's :class:`PoolInstance` IN THE SAME bundle and adds a
    NORMAL peer target to the pool's target store. Misses are loud
    (addressing constitution — no silent fallback): a sender or peer
    pool absent from the bundle raises.
    """
    for pool_name, pool_links in links.items():
        instance = pools.get(pool_name)
        if instance is None:
            raise ValueError(
                f"peer link resolution: pool {pool_name!r} declares peers but "
                f"is absent from this workspace's resource bundle — links "
                f"resolve only against the owning workspace (v1 "
                f"same-workspace invariant, SPEC §5.3)"
            )
        for link in pool_links:
            peer = pools.get(link.peer_pool)
            if peer is None:
                raise ValueError(
                    f"peer link resolution: pool {pool_name!r} declares peer "
                    f"{link.peer_pool!r} which is absent from this "
                    f"workspace's resource bundle — v1 peer links are "
                    f"same-workspace only (N5); declare {link.peer_pool!r} "
                    f"in this workspace or remove the link"
                )
            instance.target_store.add(
                CommunicationTarget(
                    name=peer.root_agent_name,
                    kind=AgentCommKind.NORMAL,
                    pool_name=link.peer_pool,
                    tree_ref=peer.tree_manager,
                    description=(
                        link.peer_description or f"Peer pool {link.peer_pool}'s main agent"
                    ),
                    execution_strategy=link.peer_execution_strategy,
                )
            )


def build_agent_pool_map(
    pool_name: str,
    pool_spec: PoolSpec,
    peer_links: Sequence[PeerLink],
) -> dict[str, str]:
    """The static agent→pool routing map over the DECLARED tree.

    Own pool's agents + each peer link's declared root (the link face
    carries the peer root's name — a declaration fact). Consumed by the
    ``MODEX_AGENT_POOL_MAP`` env face (the external-pool env spec and the
    ``native_env`` hook's template).
    """
    pool_map: dict[str, str] = {pool_spec.root_agent.name: pool_name}
    for agent in pool_spec.agents:
        pool_map[agent.name] = pool_name
    for link in peer_links:
        pool_map[link.peer_agent] = link.peer_pool
    return pool_map


def build_routable_targets(
    pool_spec: PoolSpec,
    peer_links: Sequence[PeerLink],
) -> list[tuple[str, str]]:
    """The routable targets (own non-root agents + peer roots)."""
    targets: list[tuple[str, str]] = []
    root_name = pool_spec.root_agent.name
    for agent in pool_spec.agents:
        if agent.name == root_name:
            continue
        targets.append((agent.name, agent.description or f"{agent.name} subagent"))
    for link in peer_links:
        desc = link.peer_description or f"Peer pool {link.peer_pool}'s main agent"
        targets.append((link.peer_agent, desc))
    return targets


def _root_agent_of(pools_by_name: Mapping[str, PoolSpec], pool_name: str) -> AgentSpec:
    """The root agent of ``pool_name`` — exactly one, loudly otherwise.

    Boot validation (V3/V5) guarantees the shape in production; this
    guard keeps the extraction loud on unvalidated input instead of
    silently picking a candidate.
    """
    pool = pools_by_name.get(pool_name)
    if pool is None:
        raise ValueError(
            f"peer link extraction: peer pool {pool_name!r} is not declared "
            f"in the workspace — v1 peer links are same-workspace only (N5)"
        )
    roots = [agent for agent in pool.agents if agent.parent is None]
    if len(roots) != 1:
        raise ValueError(
            f"peer link extraction: pool {pool_name!r} declares "
            f"{len(roots)} root agents — exactly one root is required "
            f"(V3) for its peer face to resolve"
        )
    return roots[0]
