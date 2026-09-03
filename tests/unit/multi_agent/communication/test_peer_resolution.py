"""Peer-link resolution (SPEC §5.2/§5.3, ticket 13) — unit tests.

Covers the two halves of the FW-ized Phase-2 peer wiring:

- ``peer_links_from_declaration`` — the scope-path extraction: per-pool
  links with the peer pool's root-agent face (description + strategy),
  loud on dangling peers / rootless pools.
- ``resolve_peer_targets`` — the bundle resolution: peer NORMAL targets
  join each sender's per-agent store with the tree reference read from
  the peer's PoolInstance in the SAME bundle; misses are loud.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.communication.peer_resolution import (
    PeerLink,
    peer_links_from_declaration,
    resolve_peer_targets,
)
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.multi_agent.tools import CommunicationTargetStore
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.scope.spec import (
    AgentSpec,
    PoolSpec,
    ScopeKind,
    ScopeSpec,
    WorkspaceSpec,
)

_DECLARATION = """\
workspace:
  name: peer-test
  pools:
    default:
      peers: [review]
      agents:
        default:
          description: the default root
    review:
      peers: [default]
      agents:
        reviewer:
          description: the review root
          execution_strategy: external
          provider_kind: opencode
    coder:
      agents:
        orchestrator:
          description: no peers here
"""


def _load_declaration(text: str) -> ScopeSpec:
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
        fh.write(text)
        path = Path(fh.name)
    try:
        return load_scope_declaration(path)
    finally:
        path.unlink()


def _instance(name: str) -> tuple[PoolInstance, MagicMock]:
    """A minimal PoolInstance with a fresh target store + mock tree."""
    tree = MagicMock()
    instance = PoolInstance(
        name=name,
        media=None,
        subagent_count=0,
        pool=MagicMock(),
        broker_bridge=MagicMock(),
        tool_manager=MagicMock(),
        skill_resolver=None,
        mcp_manager=None,
        terminal_manager=None,
        root_agent_name=name,
        main_execution_strategy=ExecutionStrategyKind.REACT,
        provider=None,
        notification_service=None,
        communication_service=MagicMock(),
        tree_manager=tree,
        target_store=CommunicationTargetStore(),
        session_binding_store=None,
        requires_main_agent_tools=True,
        roster_hook_names=frozenset(),
        comm_tools_derived=False,
    )
    return instance, tree


def _spec(peers: dict[str, list[str]]) -> ScopeSpec:
    """A hand-built workspace spec: one root agent per pool."""
    pools = [
        PoolSpec(
            name=pool_name,
            peers=peer_list,
            agents=[
                AgentSpec(name=f"{pool_name}-root", description=f"{pool_name} root")
            ],
        )
        for pool_name, peer_list in peers.items()
    ]
    return ScopeSpec(
        kind=ScopeKind.WORKSPACE,
        workspace=WorkspaceSpec(name="ws", pools=pools),
    )


# ── peer_links_from_declaration ─────────────────────────────────────────


def test_extraction_covers_only_pools_with_peers_in_declaration_order() -> None:
    """Pools without peers are absent from the link map; pools with peers
    appear in declaration order with the peer's root-agent face."""
    links = peer_links_from_declaration(_load_declaration(_DECLARATION))
    assert list(links) == ["default", "review"]
    assert links["default"] == (
        PeerLink(
            peer_pool="review",
            peer_agent="reviewer",
            peer_description="the review root",
            peer_execution_strategy=ExecutionStrategyKind.EXTERNAL,
        ),
    )
    assert links["review"] == (
        PeerLink(
            peer_pool="default",
            peer_agent="default",
            peer_description="the default root",
            peer_execution_strategy=ExecutionStrategyKind.REACT,
        ),
    )


def test_extraction_of_pool_as_root_declaration_is_empty() -> None:
    """Pool-as-root declarations carry no peer links (V5 refuses peers
    there) — extraction yields an empty map, not an error."""
    spec = ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(name="solo", agents=[AgentSpec(name="root")]),
    )
    assert peer_links_from_declaration(spec) == {}


def test_extraction_dangling_peer_is_loud() -> None:
    """A peer referencing a pool the workspace does not declare (the v1
    cross-workspace shape) fails loudly at extraction, not silently."""
    spec = _spec({"alpha": ["ghost"]})
    with pytest.raises(ValueError, match="same-workspace only"):
        peer_links_from_declaration(spec)


def test_extraction_rootless_peer_pool_is_loud() -> None:
    """A peer pool without exactly one root agent (V3 violation on
    unvalidated input) fails loudly instead of picking a candidate."""
    spec = ScopeSpec(
        kind=ScopeKind.WORKSPACE,
        workspace=WorkspaceSpec(
            name="ws",
            pools=[
                PoolSpec(name="alpha", peers=["beta"], agents=[AgentSpec(name="a")]),
                PoolSpec(name="beta", agents=[]),
            ],
        ),
    )
    with pytest.raises(ValueError, match="root"):
        peer_links_from_declaration(spec)


# ── resolve_peer_targets ────────────────────────────────────────────────


def test_resolution_adds_bidirectional_targets_from_same_bundle() -> None:
    """Both directions wire: each store gains the peer's NORMAL target
    with the tree reference read from the peer's OWN instance in the
    bundle (the same-workspace invariant — both endpoints ride one
    bundle)."""
    alpha, alpha_tree = _instance("alpha")
    beta, beta_tree = _instance("beta")
    links = {
        "alpha": (PeerLink(peer_pool="beta", peer_agent="beta-main", peer_description="beta root"),),
        "beta": (PeerLink(peer_pool="alpha", peer_agent="alpha-main", peer_description="alpha root"),),
    }

    resolve_peer_targets({"alpha": alpha, "beta": beta}, links)

    target = alpha.target_store.get("beta")
    assert target is not None
    assert target.kind is AgentCommKind.NORMAL
    assert target.pool_name == "beta"
    assert target.tree_ref is beta_tree
    assert target.description == "beta root"
    assert target.execution_strategy is ExecutionStrategyKind.REACT
    reciprocal = beta.target_store.get("alpha")
    assert reciprocal is not None
    assert reciprocal.tree_ref is alpha_tree
    assert reciprocal.pool_name == "alpha"


def test_resolution_uses_booted_main_agent_name_and_description_fallback() -> None:
    """The target name comes from the bundle instance (the booted root),
    not the link; an empty declared description falls back to the
    legacy Phase-2 wording."""
    alpha, _ = _instance("alpha")
    beta, _ = _instance("beta")
    beta.root_agent_name = "reviewer"

    resolve_peer_targets(
        {"alpha": alpha, "beta": beta},
        {"alpha": (PeerLink(peer_pool="beta", peer_agent="beta-main"),)},
    )

    target = alpha.target_store.get("reviewer")
    assert target is not None
    assert target.description == "Peer pool beta's main agent"


def test_resolution_sender_missing_from_bundle_is_loud() -> None:
    """A link for a pool absent from the bundle raises — links resolve
    only against the owning workspace's bundle."""
    beta, _ = _instance("beta")
    with pytest.raises(
        ValueError, match="absent from this workspace's resource bundle"
    ):
        resolve_peer_targets(
            {"beta": beta},
            {"alpha": (PeerLink(peer_pool="beta", peer_agent="beta-main"),)},
        )


def test_resolution_peer_missing_from_bundle_is_loud() -> None:
    """A peer absent from the bundle (the runtime cross-workspace shape)
    raises with the v1 rule named — no silent skip."""
    alpha, _ = _instance("alpha")
    with pytest.raises(ValueError, match="same-workspace only"):
        resolve_peer_targets(
            {"alpha": alpha},
            {"alpha": (PeerLink(peer_pool="ghost", peer_agent="ghost-main"),)},
        )


def test_resolution_duplicate_target_name_propagates_store_error() -> None:
    """Two peers whose booted roots share a name surface the store's
    duplicate-target ValueError (target names must be unique across
    reachable pools)."""
    alpha, _ = _instance("alpha")
    beta, _ = _instance("beta")
    gamma, _ = _instance("gamma")
    beta.root_agent_name = "dupe"
    gamma.root_agent_name = "dupe"

    with pytest.raises(ValueError, match="Duplicate communication target name"):
        resolve_peer_targets(
            {"alpha": alpha, "beta": beta, "gamma": gamma},
            {"alpha": (PeerLink(peer_pool="beta", peer_agent="beta-main"), PeerLink(peer_pool="gamma", peer_agent="gamma-main"))},
        )
