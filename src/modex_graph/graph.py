"""`Graph[S]` builder — node registry + edges + conditional edges + `compile()`.

Per ADR-0033 D6 + D9.1: a mutable builder that collects nodes and edges,
then validates and freezes them via `compile() -> CompiledGraph`.

Four routing mechanisms coexist (strict priority, resolved by the engine):

1. `Command(goto=...)` — runtime dynamic routing (highest priority).
2. `transition: str` — static edge lookup via `add_edge(src, dst, reason=)`.
3. `add_conditional_edges(src, route_fn, destinations)` — multi-candidate.
4. Default edge (`add_edge(src, dst, reason=None)`) — fallback (lowest).

`add_conditional_edges` supports two modes:

- `destinations=None`: `route_fn(state)` return value is used directly as
  the next node name.
- `destinations={"key": "node_name"}`: `route_fn(state)` returns a key, and
  the mapped node name is used. Decouples routing logic from node names.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from typing_extensions import TypeVar

from .constants import GraphNode
from .node import Node

if TYPE_CHECKING:
    from .compiled_graph import CompiledGraph
    from .state import GraphState

S = TypeVar("S", bound="GraphState")


@dataclass(frozen=True)
class Edge:
    """Directed edge. `reason=None` means unconditional fallback (default edge).

    Per ADR-0033 D6: edges are matched by exact `reason` first, then by
    `reason=None` (fallback). The engine tries `transition` (static edge
    lookup) before conditional edges before default edges.
    """

    source: str
    target: str
    reason: str | None = None


@dataclass(frozen=True)
class ConditionalEdge:
    """Conditional edge: `route_fn(state) -> str` selects the next node.

    `destinations=None`: route_fn return value is the node name directly.
    `destinations={"key": "node"}`: route_fn returns a key, mapped to a node.
    """

    source: str
    route_fn: Callable[[Any], str]
    destinations: dict[str, str] | None = None


class Graph[S: "GraphState"]:
    """Mutable graph builder. Collects nodes + edges, then `compile()` validates.

    Per ADR-0033 D9.1: business modules construct their own `Graph` instances
    from core primitives. The graph package never imports business builders.

    Usage:

    ```python
    g: Graph[MyState] = Graph()
    g.add_node("start", StartNode())
    g.add_node("llm", LLMNode())
    g.add_edge(GraphNode.START, "start")
    g.add_edge("start", "llm", reason="begin")
    g.add_edge("llm", GraphNode.END, reason=None)  # default edge
    compiled = g.compile(max_iterations=100)
    ```

    The graph is ALSO a `Node` (Graph-is-a-Node, D8): `CompiledGraph`
    subclasses `Node`, enabling subgraph nesting. Phase a wires the type
    relationship; Phase c exercises it.
    """

    def __init__(self, name: str = "graph") -> None:
        self.name: str = name
        self._nodes: dict[str, Node[S]] = {}
        self._edges: list[Edge] = []
        self._conditional_edges: list[ConditionalEdge] = []

    # ── Node registry ──────────────────────────────────────────────────

    def add_node(self, name: str, node: Node[S]) -> None:
        """Register `node` under `name`. Sets `node.name = name`."""
        # Use object.__setattr__ to support both regular Node instances and
        # frozen dataclass subclasses (e.g. CompiledGraph used as a subgraph node).
        object.__setattr__(node, "name", name)
        self._nodes[name] = node

    def get_node(self, name: str) -> Node[S]:
        """Return the node registered under `name`. Raises KeyError if absent."""
        return self._nodes[name]

    @property
    def nodes(self) -> dict[str, Node[S]]:
        """Read-only view of registered nodes."""
        return dict(self._nodes)

    # ── Edges ──────────────────────────────────────────────────────────

    def add_edge(self, source: str, target: str, reason: str | None = None) -> None:
        """Add a directed edge.

        `source` may be `GraphNode.START` (declares the entry node).
        `target` may be `GraphNode.END` (declares a terminal transition).
        `reason=None` means unconditional fallback (default edge).
        """
        self._edges.append(Edge(source=source, target=target, reason=reason))

    def add_conditional_edges(
        self,
        source: str,
        route_fn: Callable[[Any], str],
        destinations: dict[str, str] | None = None,
    ) -> None:
        """Add a conditional edge.

        `route_fn(state) -> str` is called by the engine when:
        - The current node's `NodeResult.transition` is None, AND
        - No `Command.goto` is set.

        `destinations=None`: route_fn return value is the node name directly.
        `destinations={"key": "node"}`: route_fn returns a key, mapped to a node.
        This decouples routing logic from concrete node names.
        """
        self._conditional_edges.append(
            ConditionalEdge(source=source, route_fn=route_fn, destinations=destinations)
        )

    @property
    def edges(self) -> list[Edge]:
        """Read-only view of edges."""
        return list(self._edges)

    @property
    def conditional_edges(self) -> list[ConditionalEdge]:
        """Read-only view of conditional edges."""
        return list(self._conditional_edges)

    # ── Compile ────────────────────────────────────────────────────────

    def compile(
        self,
        max_iterations: int = 100,
        *,
        cycle_detection: str = "warn",
    ) -> CompiledGraph[S]:
        """Validate and freeze this graph into a `CompiledGraph`.

        Validation:
        - Exactly one entry node (exactly one edge from `GraphNode.START`).
        - All edge sources/targets exist (except START/END sentinels).
        - No dangling edges (every source must be a registered node or START).
        - Node names unique (enforced by `add_node` dict semantics).
        - Cycle detection: `cycle_detection="warn"` (default) logs cycles but
          does not raise; `"raise"` raises `RoutingError` on any back-edge;
          `"off"` skips detection.

        `max_iterations` is the engine-level safety net (ADR-0033 D9.3):
        exceeding it raises `GraphRecursionError` (abnormal exit). Should be
        larger than the business-level max (e.g. business 25, compile 100).
        """
        from .compiled_graph import CompiledGraph

        # 1. Exactly one entry node (exactly one edge from GraphNode.START).
        start_edges = [e for e in self._edges if e.source == GraphNode.START]
        if len(start_edges) == 0:
            raise RoutingError(
                "Graph has no entry node: add an edge from GraphNode.START to your entry node."
            )
        if len(start_edges) > 1:
            raise RoutingError(
                f"Graph has multiple entry nodes ({len(start_edges)} edges from "
                f"GraphNode.START). Exactly one is required."
            )
        entry_node = start_edges[0].target
        if entry_node == GraphNode.END:
            raise RoutingError("Entry node cannot be GraphNode.END.")
        if entry_node not in self._nodes:
            raise RoutingError(f"Entry node {entry_node!r} is not a registered node.")

        # 2. All edge sources/targets exist (except START/END sentinels).
        for edge in self._edges:
            if edge.source != GraphNode.START and edge.source not in self._nodes:
                raise RoutingError(
                    f"Edge source {edge.source!r} is not a registered node "
                    f"(edge: {edge.source!r} → {edge.target!r})."
                )
            if edge.target != GraphNode.END and edge.target not in self._nodes:
                raise RoutingError(
                    f"Edge target {edge.target!r} is not a registered node "
                    f"(edge: {edge.source!r} → {edge.target!r})."
                )

        # 3. Conditional edge sources exist; destinations map to real nodes.
        for cond in self._conditional_edges:
            if cond.source not in self._nodes:
                raise RoutingError(
                    f"Conditional edge source {cond.source!r} is not a registered node."
                )
            if cond.destinations is not None:
                for key, target in cond.destinations.items():
                    if target != GraphNode.END and target not in self._nodes:
                        raise RoutingError(
                            f"Conditional edge destination for key {key!r} "
                            f"points to unregistered node {target!r}."
                        )

        # 4. Node names unique — enforced by dict semantics. Sanity check:
        # the name attribute on each node matches its registration key.
        for name, node in self._nodes.items():
            if node.name != name:
                # This would only happen if the same node instance was
                # registered under two different names. Defensive check.
                raise RoutingError(f"Node registered as {name!r} has name attribute {node.name!r}.")

        # 5. Cycle detection (optional, warn default).
        cycles = self._detect_cycles(entry_node)
        if cycles:
            msg = f"Graph contains cycles: {cycles}"
            if cycle_detection == "raise":
                raise RoutingError(msg)
            elif cycle_detection == "warn":
                # Warn but do not raise — ReAct's LLM↔TOOL loop is intentional.
                import warnings

                warnings.warn(msg, stacklevel=2)

        return CompiledGraph(
            name=self.name,
            nodes=dict(self._nodes),
            edges=list(self._edges),
            conditional_edges=list(self._conditional_edges),
            entry_node=entry_node,
            max_iterations=max_iterations,
        )

    def _detect_cycles(self, entry_node: str) -> list[list[str]]:
        """Detect cycles in the graph via DFS. Returns a list of cycles.

        Each cycle is a list of node names forming the cycle. START/END
        sentinels are excluded from cycle detection (they're terminal points).
        """
        # Build adjacency list (exclude START/END as nodes; they're sentinels).
        adj: dict[str, list[str]] = {n: [] for n in self._nodes}
        for edge in self._edges:
            if edge.source in adj and edge.target in self._nodes:
                adj[edge.source].append(edge.target)
        for cond in self._conditional_edges:
            if cond.source in adj:
                if cond.destinations:
                    adj[cond.source].extend(
                        t for t in cond.destinations.values() if t in self._nodes
                    )
                else:
                    # Can't statically know destinations; assume all nodes reachable.
                    adj[cond.source].extend(self._nodes.keys())

        cycles: list[list[str]] = []
        visited: set[str] = set()
        stack: list[str] = []
        on_stack: set[str] = set()

        def dfs(node: str) -> None:
            if node in on_stack:
                # Found a cycle — extract it from the stack.
                idx = stack.index(node)
                cycles.append(stack[idx:] + [node])
                return
            if node in visited:
                return
            visited.add(node)
            on_stack.add(node)
            stack.append(node)
            for neighbor in adj.get(node, []):
                dfs(neighbor)
            stack.pop()
            on_stack.discard(node)

        dfs(entry_node)
        return cycles


# Imported here to avoid a circular import at module load — RoutingError is
# used in compile() validation and is defined in .exceptions.
from .exceptions import RoutingError  # noqa: E402

__all__ = ["ConditionalEdge", "Edge", "Graph"]
