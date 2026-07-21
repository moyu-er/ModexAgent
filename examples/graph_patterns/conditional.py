"""Conditional-branching graph patterns over `modex_graph`.

Reusable `Node[S]` subclasses that compose `NodeResult(transition=...)` with
static edges (declared via `Graph.add_edge(source, target, reason=...)`)
into if/else and multi-branch topologies.

This is example code (lives under `examples/` per ADR-0007 rule 9). It uses
only the public `modex_graph` API — no framework-internal hooks, no
`modex_agent` imports. The patterns are generic over any `GraphState`
subclass.
"""

from __future__ import annotations

from collections.abc import Callable

from modex_graph import (
    Graph,
    GraphContext,
    GraphNode,
    GraphState,
    Node,
    NodeResult,
)


class ConditionalNode[S: GraphState](Node[S]):
    """Single-predicate if/else node.

    `execute` returns `NodeResult(transition=predicate(ctx.state))`. The
    graph topology (declared via `add_edge(source, target, reason=...)`)
    routes to the matching branch — the predicate's return value is the
    static-edge `reason` key.

    Example::

        g.add_node("decide", ConditionalNode(lambda s: "high" if s.x > 0 else "low"))
        g.add_edge("decide", "high", reason="high")
        g.add_edge("decide", "low", reason="low")
    """

    def __init__(self, predicate: Callable[[S], str]) -> None:
        self.predicate = predicate

    def execute(self, ctx: GraphContext[S]) -> NodeResult:
        return NodeResult(transition=self.predicate(ctx.state))


class SwitchNode[S: GraphState](Node[S]):
    """Multi-branch switch node.

    Each entry in `cases` is a `(transition_key, predicate)` pair. Cases are
    evaluated in `dict` insertion order; the first matching predicate's key
    becomes the `NodeResult.transition`. If no case matches, `default` is
    used. Route via static edges keyed by each `transition_key`.

    Example::

        g.add_node("switch", SwitchNode(
            cases={"a": lambda s: s.x < 5, "b": lambda s: s.x < 10},
            default="c",
        ))
        g.add_edge("switch", "a", reason="a")
        g.add_edge("switch", "b", reason="b")
        g.add_edge("switch", "c", reason="c")
    """

    def __init__(
        self,
        cases: dict[str, Callable[[S], bool]],
        default: str,
    ) -> None:
        self.cases = cases
        self.default = default

    def execute(self, ctx: GraphContext[S]) -> NodeResult:
        for key, predicate in self.cases.items():
            if predicate(ctx.state):
                return NodeResult(transition=key)
        return NodeResult(transition=self.default)


def build_conditional_graph[S: GraphState](
    predicate: Callable[[S], str],
    high_branch: Node[S],
    low_branch: Node[S],
    merge: Node[S],
) -> Graph[S]:
    """Build an if/else + merge topology.

    Topology::

        START -> conditional -> (high | low) -> merge -> END

    The `predicate` must return ``"high"`` or ``"low"``; the returned
    transition routes to the matching branch via static edges
    (`reason="high"` / `reason="low"`). Both branches converge on `merge`
    via default edges (`reason=None`), demonstrating that static-edge
    branches can merge into a common downstream node.

    Returns the uncompiled `Graph[S]` — call `.compile()` then pass to
    `GraphEngine` to execute.
    """

    g: Graph[S] = Graph()
    g.add_node("conditional", ConditionalNode(predicate))
    g.add_node("high", high_branch)
    g.add_node("low", low_branch)
    g.add_node("merge", merge)
    g.add_edge(GraphNode.START, "conditional")
    g.add_edge("conditional", "high", reason="high")
    g.add_edge("conditional", "low", reason="low")
    g.add_edge("high", "merge", reason=None)
    g.add_edge("low", "merge", reason=None)
    g.add_edge("merge", GraphNode.END, reason=None)
    return g


__all__ = [
    "ConditionalNode",
    "SwitchNode",
    "build_conditional_graph",
]
