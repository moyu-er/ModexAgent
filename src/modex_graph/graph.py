"""`Graph[S]` builder — node registry + edges + `compile()`.

Per ADR-0033 D6 + D9.1: a mutable builder that collects nodes and edges,
then validates and freezes them via `compile() -> CompiledGraph`.

Deliver-only routing (P3.4b convergence): nodes call
``deliver(content, next_node, ctx)`` during ``execute()`` to route. Edges
declare topology (which nodes can connect) and are used for:

- Compile-time validation (reachability, cycle detection).
- Runtime ``next_node=None`` resolution via ``_resolve_default_target``
  (returns all downstream targets from the current node).

The former ``reason``-based transition model and ``Command``/``Task``
dynamic routing were removed as dead code — ``deliver``/``submit`` is the
sole routing mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from typing_extensions import TypeVar

from .constants import GraphNode, NodeTrigger, SchedulerKind
from .node import Node
from .utils import generate_id

if TYPE_CHECKING:
    from .compiled_graph import CompiledGraph
    from .state import GraphState

S = TypeVar("S", bound="GraphState")


@dataclass(frozen=True)
class Edge:
    """Directed edge: ``source`` → ``target``.

    Per ADR-0033 D6: edges declare topology. Routing is deliver-only —
    nodes call ``deliver(content, next_node, ctx)`` to route at runtime.
    When ``next_node=None``, ``_resolve_default_target`` returns all
    downstream edge targets from the current node.
    """

    source: str
    target: str


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
    g.add_edge("start", "llm")
    g.add_edge("llm", GraphNode.END)
    compiled = g.compile(max_iterations=100)
    ```

    The graph is ALSO a `Node` (Graph-is-a-Node, D8): `CompiledGraph`
    subclasses `Node`, enabling subgraph nesting. The type relationship is
    wired (ADR-0033 D8); exercising it with real subgraph patterns is
    deferred (ADR-0033 D12 Phase c item 5).
    """

    def __init__(self, name: str = "graph") -> None:
        self.name: str = name
        self._nodes: dict[str, Node[S]] = {}
        self._edges: list[Edge] = []
        from .nodes.end_node import EndNode
        from .nodes.start_node import StartNode

        self.add_node(GraphNode.START, StartNode())
        self.add_node(GraphNode.END, EndNode())

    # ── Node registry ──────────────────────────────────────────────────

    def add_node(self, name: str, node: Node[S]) -> None:
        """Register `node` under `name`. Sets `node.name = name`."""
        node.name = name
        if not node.node_id:
            node.node_id = generate_id(prefix="node")
        self._nodes[name] = node

    def get_node(self, name: str) -> Node[S]:
        """Return the node registered under `name`. Raises KeyError if absent."""
        return self._nodes[name]

    @property
    def nodes(self) -> dict[str, Node[S]]:
        """Read-only view of registered nodes."""
        return dict(self._nodes)

    # ── Edges ──────────────────────────────────────────────────────────

    def add_edge(self, source: str, target: str) -> None:
        """Add a directed edge.

        `source` may be `GraphNode.START` (declares the entry node).
        `target` may be `GraphNode.END` (declares a terminal transition).
        """
        self._edges.append(Edge(source=source, target=target))

    @property
    def edges(self) -> list[Edge]:
        """Read-only view of edges."""
        return list(self._edges)

    # ── Compile ────────────────────────────────────────────────────────

    def compile(
        self,
        max_iterations: int = 100,
        *,
        cycle_detection: str = "warn",
        scheduler: SchedulerKind = SchedulerKind.LINEAR,
        default_trigger: NodeTrigger = NodeTrigger.ON_ALL_PREDS,
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

        `scheduler` selects the execution strategy (`SchedulerKind`).
        Defaults to `LINEAR` (sequential execution — the original behaviour).
        `PARALLEL` is reserved for the forthcoming `ParallelScheduler`.

        `default_trigger` (Task 06) is the graph-level default trigger mode
        under `ParallelScheduler`. A per-node `Node.trigger` overrides it
        for that node. Ignored under `LINEAR`.

        `ON_RECEIVE` is deprecated — `Graph.compile()` emits a
        `DeprecationWarning` when `default_trigger` or any registered node's
        `trigger` is `NodeTrigger.ON_RECEIVE`. Use `ON_ALL_PREDS` for
        production graphs. `GraphSpec` (declarative API) rejects
        `ON_RECEIVE` entirely.
        """
        import warnings

        if default_trigger == NodeTrigger.ON_RECEIVE:
            warnings.warn(
                "NodeTrigger.ON_RECEIVE is deprecated/experimental and is not "
                "part of the stable scheduling contract. Use "
                "NodeTrigger.ON_ALL_PREDS for production graphs.",
                DeprecationWarning,
                stacklevel=2,
            )
        on_receive_nodes = [
            name for name, node in self._nodes.items()
            if node.trigger == NodeTrigger.ON_RECEIVE
        ]
        if on_receive_nodes:
            warnings.warn(
                f"Nodes {on_receive_nodes} declare trigger=ON_RECEIVE, which is "
                "deprecated/experimental. Use ON_ALL_PREDS for production graphs.",
                DeprecationWarning,
                stacklevel=2,
            )
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
        entry_target = start_edges[0].target
        if entry_target not in self._nodes:
            raise RoutingError(f"Entry node {entry_target!r} is not a registered node.")

        # 2. All edge sources/targets are registered executable nodes.
        for edge in self._edges:
            if edge.source not in self._nodes:
                raise RoutingError(
                    f"Edge source {edge.source!r} is not a registered node "
                    f"(edge: {edge.source!r} → {edge.target!r})."
                )
            if edge.target not in self._nodes:
                raise RoutingError(
                    f"Edge target {edge.target!r} is not a registered node "
                    f"(edge: {edge.source!r} → {edge.target!r})."
                )

        # 3. Node names unique — enforced by dict semantics. Sanity check:
        # the name attribute on each node matches its registration key.
        for name, node in self._nodes.items():
            if node.name != name:
                # This would only happen if the same node instance was
                # registered under two different names. Defensive check.
                raise RoutingError(f"Node registered as {name!r} has name attribute {node.name!r}.")

        # 4. Cycle detection (optional, warn default).
        cycles = self._detect_cycles(GraphNode.START)
        if cycles:
            msg = f"Graph contains cycles: {cycles}"
            if cycle_detection == "raise":
                raise RoutingError(msg)
            elif cycle_detection == "warn":
                # Warn but do not raise — ReAct's LLM↔TOOL loop is intentional.
                import warnings

                warnings.warn(msg, stacklevel=2)

        # 5. START/END reachability validation (PARALLEL only, Task 07).
        # Under PARALLEL, every registered node must be reachable from the
        # entry node (forward BFS) and must be able to reach GraphNode.END
        # (reverse BFS from END). LINEAR skips both checks — it follows a
        # single path and unreachable nodes are simply never visited.
        if scheduler == SchedulerKind.PARALLEL:
            self._validate_reachability(GraphNode.START)

        return CompiledGraph(
            name=self.name,
            nodes=dict(self._nodes),
            edges=list(self._edges),
            entry_node=GraphNode.START,
            max_iterations=max_iterations,
            scheduler=scheduler,
            default_trigger=default_trigger,
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

    def _validate_reachability(self, entry_node: str) -> None:
        """Validate START/END reachability for PARALLEL scheduler (Task 07).

        - START reachability: forward BFS from `entry_node` along declared
          outgoing edges. Every registered node must be visited.
        - END reachability: reverse BFS from `GraphNode.END` along incoming
          edges. Every registered node must be visited (i.e., can reach END).

        `GraphNode.START` and `GraphNode.END` are sentinels, not registered
        nodes — they're excluded from the visited set but used as BFS
        start points.
        """
        forward_adj: dict[str, set[str]] = {n: set() for n in self._nodes}
        for edge in self._edges:
            if edge.source in forward_adj and edge.target in self._nodes:
                forward_adj[edge.source].add(edge.target)

        visited_from_start: set[str] = set()
        queue: list[str] = [entry_node]
        while queue:
            current = queue.pop(0)
            if current in visited_from_start:
                continue
            visited_from_start.add(current)
            for neighbor in forward_adj[current]:
                if neighbor not in visited_from_start:
                    queue.append(neighbor)

        unreachable = set(self._nodes) - {GraphNode.START, GraphNode.END} - visited_from_start
        if unreachable:
            raise RoutingError(
                f"Nodes unreachable from entry node {entry_node!r}: "
                f"{sorted(unreachable)}. Under PARALLEL scheduler, all "
                f"registered nodes must be reachable from the entry node "
                f"via declared edges."
            )

        reverse_adj: dict[str, set[str]] = {}
        for edge in self._edges:
            reverse_adj.setdefault(edge.target, set()).add(edge.source)

        visited_to_end: set[str] = {GraphNode.END}
        queue = [GraphNode.END]
        while queue:
            current = queue.pop(0)
            for predecessor in reverse_adj.get(current, ()):
                if predecessor in self._nodes and predecessor not in visited_to_end:
                    visited_to_end.add(predecessor)
                    queue.append(predecessor)

        cant_reach_end = set(self._nodes) - visited_to_end
        if cant_reach_end:
            raise RoutingError(
                f"Nodes that cannot reach {GraphNode.END!r}: "
                f"{sorted(cant_reach_end)}. Under PARALLEL scheduler, all "
                f"registered nodes must have a path to END via declared edges."
            )


# Imported here to avoid a circular import at module load — RoutingError is
# used in compile() validation and is defined in .exceptions.
from .exceptions import RoutingError  # noqa: E402

__all__ = ["Edge", "Graph"]
