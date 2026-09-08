"""Bootstrap: query store -> produce seed node names.

The caller passes an explicit ``mode``:

- ``BootstrapMode.FRESH`` — new run or re-invoke. Zero scanning; returns
  ``[graph.entry_node]`` immediately. No auto-promote, no seed derivation.
- ``BootstrapMode.RECOVERY`` — crash/pause recovery. Full derivation:
  auto-promote STAGED + CONSUMED_PENDING delivers for COMPLETED nodes BEFORE
  seed derivation, then derive seeds from CRASHED/orphan RUNNING/CANCELED invocations
  and PENDING delivers, ordered topologically (BFS from ``entry_node``).

Side effects (RECOVERY only):

- Auto-promotes STAGED delivers whose source node is COMPLETED (crash
  between ``complete_invocation`` and ``promote_staged_by_source``),
  before seed derivation so promoted PENDING rows are visible to the
  pending-deliver seed scan.
- Auto-promotes CONSUMED_PENDING delivers whose consuming invocation is
  COMPLETED (crash between ``mark_consumed`` and ``promote_delivers``),
  also before seed derivation.

``bootstrap`` does NOT create instances or mark anything READY. The scheduler
receives the seed list and decides how to use it (LinearScheduler takes
``seeds[0]``; ParallelScheduler creates instances for re-execute and
fresh-start seeds, while PENDING-deliver seeds are discovered by
``_recheck_pending``'s store scan).
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from ..constants import DeliverConsumptionStatus, GraphNode, InvocationStatus

if TYPE_CHECKING:
    from typing_extensions import TypeVar

    from ..compiled_graph import CompiledGraph
    from ..context import GraphContext
    from ..persistence.deliver_store import DeliverRecord
    from ..persistence.graph_metadata import GraphMetadata, NodeInvocationRecord
    from ..state import GraphState

    S = TypeVar("S", bound="GraphState")


class BootstrapMode(StrEnum):
    """Explicit bootstrap mode — the caller MUST pass one (no default).

    - ``FRESH`` — ``start_run`` first run, ``start_invoke`` re-invoke.
      Zero scanning; returns ``[entry_node]``.
    - ``RECOVERY`` — ``recover_crashed``, orphan pickup, ``resume`` from
      PAUSED. Full derivation from persisted invocation + deliver state.
    """

    FRESH = "fresh"
    RECOVERY = "recovery"


# The original graph version at fresh admission. Retries retain this membership;
# node/IO primary keys have no ordering or provenance role.
GRAPH_RUN_VERSION_KEY = "graph_run_version"


def graph_run_version(metadata: GraphMetadata | None) -> int | None:
    """Read durable logical membership; absent values identify legacy unscoped runs."""
    value = metadata.attrs.get(GRAPH_RUN_VERSION_KEY) if metadata is not None else None
    # attrs is the persisted extension boundary, not an untyped runtime fallback.
    if value is not None and not isinstance(value, int):
        raise ValueError("graph_run_version must be an integer or None")
    return value


def bootstrap[S: "GraphState"](
    ctx: GraphContext[S],
    graph: CompiledGraph[S],
    *,
    mode: BootstrapMode,
) -> list[str]:
    """Query store -> produce seed node names.

    Args:
        ctx: The graph context (coordinator + state).
        graph: The compiled graph.
        mode: ``FRESH`` (zero scanning, return ``[entry_node]``) or
            ``RECOVERY`` (full derivation from persisted state).

    Returns:
        Seed node names ordered topologically (BFS from ``entry_node``).
        Empty-seed ``RECOVERY`` returns ``[]`` when matching run history exists;
        without history it returns ``[entry_node]``, as does ``FRESH``.
    """
    if mode is BootstrapMode.FRESH:
        return [graph.entry_node]

    # ── RECOVERY mode: full derivation ───────────────────────────────

    coordinator = ctx.coordinator
    node_state_store = coordinator.node_state_store

    run_version = ctx.graph_run_version
    if run_version is not None:
        start = node_state_store.load_latest(graph.nodes[graph.entry_node].node_id)
        if start is None or start.graph_run_version != run_version:
            ctx.reached_end = False
            return [graph.entry_node]

    # 1. Auto-promote DOUBLE — both BEFORE seed derivation so promoted
    #    PENDING rows are visible to the pending-deliver seed scan.
    #    START and END are included: either can have leftover STAGED rows
    #    or CONSUMED_PENDING delivers that need promotion.

    # 1a. Find COMPLETED invocations + promote their STAGED delivers.
    completed_invocations: set[int] = set()
    invocation_records: dict[str, NodeInvocationRecord | None] = {}
    for name, node in graph.nodes.items():
        record = node_state_store.load_latest(node.node_id)
        if record is not None and record.graph_run_version != run_version:
            record = None
        invocation_records[name] = record
        if record is not None and record.status == InvocationStatus.COMPLETED:
            completed_invocations.add(record.invocation_id)
            coordinator.promote_staged_by_source(coordinator.graph_instance_id, node.node_id)

    # 1b. Collect consumable delivers AFTER staged promotion (so the cache
    #     sees newly-promoted PENDING rows). Reuse this cache for both the
    #     CONSUMED_PENDING auto-promote and the PENDING-deliver seed scan
    #     (single scan per node instead of two).
    #     Auto-promote CONSUMED_PENDING delivers whose consuming invocation
    #     is COMPLETED (crash between mark_consumed and promote_delivers).
    node_delivers_cache: dict[str, list[DeliverRecord]] = {}
    for name, node in graph.nodes.items():
        if name == GraphNode.START:
            continue
        delivers = coordinator.collect_consumable_delivers(node.node_id, 0)
        node_delivers_cache[name] = delivers
        if completed_invocations:
            for d in delivers:
                if (
                    d.status == DeliverConsumptionStatus.CONSUMED_PENDING
                    and d.consumed_by_invocation_id is not None
                    and d.consumed_by_invocation_id in completed_invocations
                ):
                    coordinator.promote_delivers(node.node_id, d.consumed_by_invocation_id)

    # 2. Seed derivation.
    #    (a) CRASHED / orphan RUNNING / CANCELED invocations -> re-execution.
    #    (b) Nodes with PENDING delivers -> seed (reuse delivers cache).
    seeds: list[str] = []

    for name in graph.nodes:
        record = invocation_records.get(name)
        if record is None:
            continue
        if record.status == InvocationStatus.COMPLETED:
            continue
        # CRASHED / orphan RUNNING / CANCELED -> fresh invocation, same inputs.
        seeds.append(name)

    for name in graph.nodes:
        if name == GraphNode.START or name in seeds:
            continue
        delivers = node_delivers_cache[name]
        if any(d.status == DeliverConsumptionStatus.PENDING for d in delivers):
            seeds.append(name)

    # Recovery may enter END directly or find it already completed. Preserve
    # the terminal routing fact without replaying completed work or its result.
    end_record = invocation_records.get(GraphNode.END)
    ctx.reached_end = GraphNode.END in seeds or (
        not seeds and end_record is not None and end_record.status == InvocationStatus.COMPLETED
    )

    # 3. No matching invocation means this run never began (or Null stores).
    #    Existing completed work must not be replayed by a recovery fallback.
    if not seeds:
        return [] if any(invocation_records.values()) else [graph.entry_node]

    # 4. BFS ordering from entry_node (includes END as a valid BFS node
    #    for fan-in closure) so LinearScheduler can use seeds[0] as the
    #    earliest recovery point.
    seed_set = set(seeds)
    ordered: list[str] = []
    visited: set[str] = set()
    queue: list[str] = [graph.entry_node]
    while queue:
        name = queue.pop(0)
        if name in visited:
            continue
        visited.add(name)
        if name in seed_set:
            ordered.append(name)
        for edge in graph.edges_from(name):
            if edge.target not in visited:
                queue.append(edge.target)

    # Append any seeds not reached by BFS (disconnected nodes).
    ordered_set = set(ordered)
    for name in seeds:
        if name not in ordered_set:
            ordered.append(name)

    return ordered


__all__ = ["BootstrapMode", "GRAPH_RUN_VERSION_KEY", "bootstrap", "graph_run_version"]
