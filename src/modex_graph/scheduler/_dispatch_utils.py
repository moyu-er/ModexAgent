"""Shared dispatch-handler helpers — converged topology validation + deliver routing.

Both ``LinearScheduler._handle_linear_dispatch`` and
``ParallelScheduler._handle_dispatch`` perform the same two steps after
resolving the source node name:

1. **Topology validation** — target must be a declared downstream edge.
2. **Deliver routing** — extract content / source metadata from the
   ``state_update`` dict and call ``coordinator.route_deliver``.

These helpers eliminate the duplicated inline logic (rule 15: converge).
The caller is responsible for resolving ``source_node_name`` from its own
instance model (LINEAR uses node names as instance IDs; PARALLEL resolves
via ``_instances[instance_id].node_name``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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


def route_deliver_from_dispatch[S: "GraphState"](
    ctx: GraphContext[S],
    graph: CompiledGraph[S],
    source_node_name: str,
    target: str,
    state_update: dict[str, Any] | None,
) -> int | None:
    """Route a deliver to the target node's deliver_store via the coordinator.

    Extracts ``delivered`` content, ``_source_node`` (defaulting to the
    source node's ``node_id``), and ``_source_inv_id`` (defaulting to 0)
    from ``state_update``, then calls ``coordinator.route_deliver``.

    Returns the ``deliver_id`` from ``route_deliver`` (or ``None``).
    """
    content = state_update.get("delivered") if state_update else None
    source_node_id = (
        state_update.get("_source_node", graph.nodes[source_node_name].node_id)
        if state_update
        else graph.nodes[source_node_name].node_id
    )
    source_inv_id = state_update.get("_source_inv_id", 0) if state_update else 0
    target_node_id = graph.nodes[target].node_id
    return ctx.coordinator.route_deliver(
        target_node_id, content, source_node_id, source_inv_id
    )


__all__ = ["validate_dispatch_target", "route_deliver_from_dispatch"]
