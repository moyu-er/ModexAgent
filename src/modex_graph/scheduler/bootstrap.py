"""Unified bootstrap: query store -> produce seed node names.

Convergence principle: graph normal scheduling, pause recovery, and crash
recovery all rely on the same ``node/deliver`` mechanism. ``bootstrap`` is the
single entry point that both schedulers call at the top of ``run_async``. It
restores state from the store and derives seed node names that need execution.

- Fresh start (no prior invocations): returns ``[graph.entry_node]``.
- Recovery (prior invocations exist): returns nodes needing re-execution
  (CRASHED / orphan RUNNING / suspended RUNNING) plus nodes with PENDING
  delivers, ordered topologically (BFS from entry_node).

Side effects:
- Restores ``ctx.state`` from the single newest full snapshot (via
  ``coordinator.rebuild_main_state``).
- Auto-promotes CONSUMED_PENDING delivers whose consuming invocation is
  COMPLETED (crash between ``mark_consumed`` and ``promote_delivers``).

``bootstrap`` does NOT create instances or mark anything READY. The scheduler
receives the seed list and decides how to use it (LinearScheduler takes
``seeds[0]``; ParallelScheduler creates instances for re-execute and
fresh-start seeds, while PENDING-deliver seeds are discovered by
``_recheck_pending``'s store scan).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..constants import DeliverConsumptionStatus, GraphInstanceStatus, GraphNode, InvocationStatus

if TYPE_CHECKING:
    from typing_extensions import TypeVar

    from ..compiled_graph import CompiledGraph
    from ..context import GraphContext
    from ..state import GraphState

    S = TypeVar("S", bound="GraphState")


def bootstrap(ctx: GraphContext[S], graph: CompiledGraph[S]) -> list[str]:
    """Unified startup: query store -> produce seed node names.

    Returns seed node names that need execution, ordered topologically
    (BFS from ``graph.entry_node``). Fresh start returns ``[entry_node]``.
    """
    coordinator = ctx.coordinator
    node_state_store = coordinator.node_state_store

    # 1. Restore state from the single newest full snapshot.
    rebuilt = coordinator.rebuild_main_state()
    if rebuilt:
        ctx.state = type(ctx.state).model_validate(rebuilt)

    # 2. Derive seeds from per-node latest invocation status.
    seeds: list[str] = []
    completed_invocations: set[int] = set()

    for name, node in graph.nodes.items():
        if name in (GraphNode.START, GraphNode.END):
            continue
        record = node_state_store.load_latest(node.node_id)
        if record is None:
            continue
        if record.status == InvocationStatus.COMPLETED:
            completed_invocations.add(record.invocation_id)
            continue
        if record.status == InvocationStatus.CANCELED:
            continue
        # CRASHED / orphan RUNNING / suspended RUNNING -> needs re-execution.
        seeds.append(name)

    # 3. Add nodes with PENDING delivers to seeds + cache consumable delivers
    #    for the auto-promote step (single scan per node instead of two).
    node_delivers_cache: dict[str, list[Any]] = {}
    for name, node in graph.nodes.items():
        if name in (GraphNode.START, GraphNode.END):
            continue
        if name in seeds:
            continue
        delivers = coordinator.collect_consumable_delivers(node.node_id, 0)
        node_delivers_cache[name] = delivers
        if any(d.status == DeliverConsumptionStatus.PENDING for d in delivers):
            seeds.append(name)

    # 4. Auto-promote CONSUMED_PENDING delivers for COMPLETED invocations
    #    (crash between mark_consumed and promote_delivers).
#    Reuses the delivers cache from the PENDING-scan step for nodes not in seeds;
#    scans the remaining seed nodes (skipped in the PENDING-scan step) fresh.
    if completed_invocations:
        for name, node in graph.nodes.items():
            if name in (GraphNode.START, GraphNode.END):
                continue
            cached = node_delivers_cache.get(name)
            delivers = cached if cached is not None else coordinator.collect_consumable_delivers(node.node_id, 0)
            for d in delivers:
                if (
                    d.status == DeliverConsumptionStatus.CONSUMED_PENDING
                    and d.consumed_by_invocation_id is not None
                    and d.consumed_by_invocation_id in completed_invocations
                ):
                    coordinator.promote_delivers(node.node_id, d.consumed_by_invocation_id)

    # 5. No seeds (no CRASHED/RUNNING nodes, no PENDING delivers).
    #    Distinguish re-invocation from recovery:
    #    - Instance status RUNNING (begin_invocation just set it) → re-invocation
    #      → [entry_node]. Per ADR-0040: the `has_any_invocation` gate is removed
    #      for this path — prior COMPLETED node records do NOT block re-execution.
    #    - Instance status terminal (COMPLETED/FAILED/CRASHED) → recovery
    #      → [] (graph is done, nothing to execute).
    #    - Instance store returns None (Null store / no persistence) → fall back
    #      to the has_any_invocation check: fresh start (no prior node records)
    #      → [entry_node]; prior COMPLETED records → [] (recovery on Null store).
    if not seeds:
        instance_metadata = coordinator.instance_store.load(ctx.graph_instance_id)
        if instance_metadata is not None:
            if instance_metadata.status == GraphInstanceStatus.RUNNING:
                return [graph.entry_node]
            return []
        has_any_invocation = any(
            node_state_store.load_latest(node.node_id) is not None
            for name, node in graph.nodes.items()
            if name not in (GraphNode.START, GraphNode.END)
        )
        return [] if has_any_invocation else [graph.entry_node]

    # 6. Order seeds topologically (BFS from entry_node) so the LinearScheduler
    #    can use seeds[0] as the earliest recovery point.
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
            if edge.target not in visited and edge.target != GraphNode.END:
                queue.append(edge.target)

    # Append any seeds not reached by BFS (disconnected nodes).
    ordered_set = set(ordered)
    for name in seeds:
        if name not in ordered_set:
            ordered.append(name)

    return ordered


__all__ = ["bootstrap"]
