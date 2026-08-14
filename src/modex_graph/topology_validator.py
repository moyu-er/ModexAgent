"""`TopologyValidator` + `TopologyError` — pure deterministic graph validation.

`TopologyValidator` is a pure, deterministic, side-effect-free
validator that runs AFTER `GraphSpec`'s basic structural validation (which
lives in `GraphSpec._validate_structure`) and BEFORE `Graph.compile()`.

It validates the deeper topology concerns that `GraphSpec` deliberately
defers to P2:

1. **Node whitelist** — every edge endpoint is a declared node name or a
   `GraphNode` sentinel (START/END). `GraphSpec` already checks this, but
   `TopologyValidator` re-checks to be self-contained.
2. **Entry edge** — exactly one edge from `GraphNode.START` to a real node.
   `GraphSpec` only requires "at least one"; the validator enforces "exactly
   one".
3. **Exit edge** — at least one edge to `GraphNode.END`.
4. **START reachability** — every declared node is reachable from the entry
   node via forward BFS along declared edges.
5. **END reachability** — every declared node can reach `GraphNode.END`
   via reverse BFS from END along incoming edges (no dead ends).
6. **Cycle detection** — cycles are ALLOWED (ReAct's LLM↔TOOL loop is
   intentional). `max_iterations` must be > 0 (re-checked here for safety).
7. **max_depth** — the longest simple path (in edges) from START to END
   must be `<= max_depth` if configured.
8. **max_nodes** — node count must be `<= max_nodes` if configured.
9. **No duplicate edges** — each `(source, target)` pair must be unique.
10. **No self-loops** — `source == target` is rejected for non-sentinel
    nodes (a real node looping to itself is a spec error; ReAct-style loops
    use two distinct nodes).

The validator is a regular class (not an ABC) — it has no abstract methods
and is not an extension point. It is a deterministic tool with no
dependencies on LLMs, I/O, or side effects. A custom validator can be
injected into `GraphSpecCompiler` for test overriding.
"""

from __future__ import annotations

from collections import deque

from .constants import GraphNode
from .spec import EdgeSpec, GraphSpec

# Frozen set of sentinel node names. Edges may reference these in addition
# to declared node names. Using a module-level frozenset avoids re-allocating
# the set on every validate() call.
_SENTINELS = frozenset({GraphNode.START, GraphNode.END})


class TopologyError(Exception):
    """Raised when graph topology validation fails.

    Distinct from `RoutingError` (raised by `Graph.compile()` at build time)
    and `GraphRecursionError` (raised by the engine at run time).
    `TopologyError` is raised at spec-validation time, before any `Graph`
    builder is invoked.
    """


class TopologyValidator:
    """Pure deterministic graph topology validator.

    No LLM, no I/O, no side effects. See module docstring for the full list
    of validation checks.

    Usage:

    ```python
    validator = TopologyValidator()
    validator.validate(spec)                       # raises TopologyError
    validator.validate(spec, max_depth=10)         # also check max_depth
    validator.validate(spec, max_nodes=50)         # also check max_nodes
    ```

    `GraphSpecCompiler` calls `validate(spec)` (with no extra limits) after
    building the `Graph` topology and before calling `graph.compile()`.
    """

    def validate(
        self,
        spec: GraphSpec,
        *,
        max_depth: int | None = None,
        max_nodes: int | None = None,
    ) -> None:
        """Validate the graph topology. Raises `TopologyError` on failure.

        Args:
            spec: the `GraphSpec` to validate. `GraphSpec`'s own
                `_validate_structure` has already run at construction time;
                this method performs the deeper topology checks.
            max_depth: optional cap on the longest simple path (in edges)
                from `GraphNode.START` to `GraphNode.END`. If `None`, no
                depth check is performed.
            max_nodes: optional cap on the number of declared nodes. If
                `None`, no node-count check is performed.
        """
        node_names = [n.name for n in spec.nodes if n.name not in _SENTINELS]
        node_name_set = set(node_names)
        sentinels = _SENTINELS

        # 1. Node whitelist — every edge endpoint is declared or sentinel.
        # (GraphSpec already checks this; re-check to be self-contained and
        # to give a TopologyError rather than a ValidationError when called
        # directly on a hand-built spec.)
        for edge in spec.edges:
            for endpoint in (edge.source, edge.target):
                if endpoint not in sentinels and endpoint not in node_name_set:
                    raise TopologyError(
                        f"Edge ({edge.source!r} → {edge.target!r}) references "
                        f"unknown node {endpoint!r}. All non-sentinel edge "
                        f"endpoints must be declared in `nodes`."
                    )

        # 2. Entry edge — exactly one edge from GraphNode.START.
        start_edges = [e for e in spec.edges if e.source == GraphNode.START]
        if len(start_edges) == 0:
            raise TopologyError(
                f"Graph has no entry edge from {GraphNode.START!r}. "
                f"Add an edge from {GraphNode.START!r} to the entry node."
            )
        if len(start_edges) > 1:
            targets = [e.target for e in start_edges]
            raise TopologyError(
                f"Graph has multiple entry edges from {GraphNode.START!r} "
                f"({len(start_edges)} edges, targets={targets}). "
                f"Exactly one entry edge is required."
            )
        entry_node = start_edges[0].target
        if entry_node == GraphNode.START:
            raise TopologyError(
                f"Entry edge target cannot be {GraphNode.START!r} — the entry must be a real node."
            )
        # entry_node is in node_name_set (the sentinel validation step guarantees this).

        # 3. Exit edge — at least one edge to GraphNode.END.
        end_edges = [e for e in spec.edges if e.target == GraphNode.END]
        if not end_edges:
            raise TopologyError(
                f"Graph has no exit edge to {GraphNode.END!r}. "
                f"Add at least one edge from a real node to {GraphNode.END!r}."
            )

        # 4. No duplicate edges — same (source, target) pair.
        seen_pairs: set[tuple[str, str]] = set()
        for edge in spec.edges:
            pair = (edge.source, edge.target)
            if pair in seen_pairs:
                raise TopologyError(
                    f"Duplicate edge ({edge.source!r} → {edge.target!r}). "
                    f"Each (source, target) pair must be unique."
                )
            seen_pairs.add(pair)

        # 5. No self-loops — source == target for non-sentinel nodes.
        # A real node looping directly to itself is a spec error. ReAct-style
        # loops use two distinct nodes (LLM → TOOL → LLM). Sentinel
        # "self-loops" (START→START, END→END) are blocked by the sentinel validation step (sentinels
        # aren't real nodes) and by the entry/exit checks, so the only case
        # that reaches here is a real-node self-loop.
        for edge in spec.edges:
            if edge.source == edge.target and edge.source not in sentinels:
                raise TopologyError(
                    f"Self-loop on node {edge.source!r} is not allowed "
                    f"(edge {edge.source!r} → {edge.target!r}). ReAct-style "
                    f"loops must use two distinct nodes."
                )

        # 6. Build adjacency (real-node → real-node only; sentinels are
        # markers, not graph nodes).
        forward_adj: dict[str, set[str]] = {n: set() for n in node_name_set}
        for edge in spec.edges:
            if edge.source in node_name_set and edge.target in node_name_set:
                forward_adj[edge.source].add(edge.target)

        # 7. START reachability — forward BFS from entry_node.
        visited_from_start: set[str] = set()
        queue: deque[str] = deque([] if entry_node == GraphNode.END else [entry_node])
        while queue:
            current = queue.popleft()
            if current in visited_from_start:
                continue
            visited_from_start.add(current)
            for neighbor in forward_adj[current]:
                if neighbor not in visited_from_start:
                    queue.append(neighbor)

        unreachable = node_name_set - visited_from_start
        if unreachable:
            raise TopologyError(
                f"Nodes unreachable from entry node {entry_node!r}: "
                f"{sorted(unreachable)}. Every declared node must be "
                f"reachable from {GraphNode.START!r} via declared edges."
            )

        # 8. END reachability — reverse BFS from nodes with direct edges to END.
        reverse_adj: dict[str, set[str]] = {n: set() for n in node_name_set}
        for edge in spec.edges:
            if edge.source in node_name_set and edge.target in node_name_set:
                reverse_adj[edge.target].add(edge.source)

        # Seed: real nodes that have a direct edge to END.
        end_seeds: set[str] = {e.source for e in end_edges if e.source in node_name_set}

        visited_to_end: set[str] = set()
        queue = deque(end_seeds)
        while queue:
            current = queue.popleft()
            if current in visited_to_end:
                continue
            visited_to_end.add(current)
            for predecessor in reverse_adj[current]:
                if predecessor not in visited_to_end:
                    queue.append(predecessor)

        cant_reach_end = node_name_set - visited_to_end
        if cant_reach_end:
            raise TopologyError(
                f"Nodes that cannot reach {GraphNode.END!r}: "
                f"{sorted(cant_reach_end)}. Every declared node must have a "
                f"path to {GraphNode.END!r} via declared edges (no dead ends)."
            )

        # 9. max_nodes — node count cap.
        if max_nodes is not None and len(node_names) > max_nodes:
            raise TopologyError(
                f"Graph has {len(node_names)} nodes, exceeding max_nodes={max_nodes}."
            )

        # 10. max_depth — longest simple path START→END.
        # Cycles are allowed, so this is the longest SIMPLE path (no repeated
        # nodes). Computed via DFS with a path-visited set. NP-hard in
        # general but fine for spec-time validation on typical graph sizes.
        if max_depth is not None:
            longest = self._longest_simple_path_to_end(entry_node, forward_adj, end_edges)
            if longest > max_depth:
                raise TopologyError(
                    f"Graph longest path from {GraphNode.START!r} to "
                    f"{GraphNode.END!r} has {longest} edges, exceeding "
                    f"max_depth={max_depth}."
                )

        # 11. Cycles allowed — just enforce max_iterations > 0.
        # GraphSpec already validates this, but re-check for safety when the
        # validator is called directly on a hand-built spec.
        if spec.max_iterations <= 0:
            raise TopologyError(
                f"GraphSpec.max_iterations must be > 0 "
                f"(got {spec.max_iterations}). Cycles are allowed but "
                f"require a positive iteration cap."
            )

    def _longest_simple_path_to_end(
        self,
        entry_node: str,
        forward_adj: dict[str, set[str]],
        end_edges: list[EdgeSpec],
    ) -> int:
        """Compute the longest simple path (in edges) from START to END.

        The path includes the START→entry edge (1 edge) and the final
        node→END edge (1 edge). A minimal graph
        (START → n1 → END) has depth 2.

        Uses DFS with a per-path visited set to avoid repeating nodes
        (simple path). Cycles are traversed only once per path — the DFS
        backtracks when it hits a node already on the current path.

        The caller guarantees that `entry_node` can reach END (the reachability check of
        `validate`), so at least one terminal node is reachable and the
        return value is `>= 2`.
        """
        # Real nodes with a direct edge to END.
        terminal_nodes: set[str] = {e.source for e in end_edges if e.source != GraphNode.START}

        max_path_edges = 0
        # Nodes on the current DFS path (avoids cycle infinite recursion).
        path_visited: set[str] = {entry_node}

        def dfs(node: str, depth_so_far: int) -> None:
            nonlocal max_path_edges
            # If this node can directly reach END, record the path length.
            # The +1 accounts for the node→END edge.
            if node in terminal_nodes:
                total = depth_so_far + 1
                if total > max_path_edges:
                    max_path_edges = total
            # Continue exploring — a terminal node may also have outgoing
            # edges leading to a longer path that eventually reaches END.
            for neighbor in forward_adj.get(node, ()):
                if neighbor not in path_visited:
                    path_visited.add(neighbor)
                    dfs(neighbor, depth_so_far + 1)
                    path_visited.discard(neighbor)

        dfs(entry_node, 0)
        # +1 for the START→entry edge (counted in the total path but not
        # in the DFS, which starts at entry_node).
        return max_path_edges + 1


__all__ = ["TopologyError", "TopologyValidator"]
