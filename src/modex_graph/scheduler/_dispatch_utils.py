"""Shared dispatch-handler helpers — topology validation + END reachability.

Both ``LinearScheduler._handle_linear_dispatch`` and
``ParallelScheduler._handle_dispatch`` perform the same two steps after
resolving the source node name:

1. **Topology validation** — target must be a declared downstream edge.
2. **END reachability** — an END wakeup marks the graph as complete.

These helpers eliminate the duplicated inline logic (rule 15: converge).
The caller is responsible for resolving ``source_node_name`` from its own
instance model (LINEAR uses node names as instance IDs; PARALLEL resolves
via ``_instances[instance_id].node_name``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..constants import GraphNode
from ..exceptions import RoutingError

if TYPE_CHECKING:
    from ..compiled_graph import CompiledGraph
    from ..context import GraphContext
    from ..state import GraphState


def validate_dispatch_target[S: "GraphState"](
    graph: CompiledGraph[S],
    source_node_name: str,
    target: str,
) -> None:
    """Validate that ``target`` is a declared downstream edge of ``source_node_name``.

    Raises ``RoutingError`` if ``target`` is not in the outgoing edges of
    ``source_node_name``. This is the topology enforcement shared by both
    schedulers.
    """
    valid_targets = {e.target for e in graph.edges_from(source_node_name)}
    if target not in valid_targets:
        raise RoutingError(
            f"Dispatch target {target!r} is not in the outgoing edges of "
            f"node {source_node_name!r}. Valid targets: "
            f"{sorted(valid_targets)}."
        )


def record_end_reachability[S: "GraphState"](
    ctx: GraphContext[S],
    target: str,
) -> None:
    """Record END reachability for a pure-wakeup dispatch.

    In the STAGED deliver model, content is persisted in the target's
    deliver store during ``execute()`` via ``deliver()`` →
    ``route_deliver(stage=True)``, then promoted STAGED→PENDING by
    ``promote_staged_by_source`` after the source invocation completes.
    The subsequent ``ctx.dispatch(target)`` is a pure
    scheduling wakeup — no content to route here. This function only
    records END reachability; the deliver is already in the store.
    """
    if target == GraphNode.END:
        ctx.reached_end = True


__all__ = ["record_end_reachability", "validate_dispatch_target"]
