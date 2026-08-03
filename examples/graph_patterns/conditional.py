"""Conditional-branching graph patterns over `modex_graph`.

Reusable `Node[S]` subclasses that use `deliver(content, target, ctx)` to
route to if/else and multi-branch topologies.

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
    IntegratedInput,
    Node,
    NodeResult,
)


class ConditionalNode[S: GraphState](Node[S]):
    """Single-predicate if/else node.

    `execute` evaluates `predicate(ctx.state)` and delivers to the matching
    branch. The graph topology (declared via `add_edge(source, target)`)
    must include both branch targets as outgoing edges from this node.

    Example::

        g.add_node("decide", ConditionalNode(lambda s: "high" if s.x > 0 else "low"))
        g.add_edge("decide", "high")
        g.add_edge("decide", "low")
    """

    def __init__(self, predicate: Callable[[S], str]) -> None:
        self.predicate = predicate

    def execute(self, ctx: GraphContext[S], integrated_input: IntegratedInput) -> NodeResult:
        target = self.predicate(ctx.state)
        self.deliver(None, target, ctx)
        return NodeResult()


class SwitchNode[S: GraphState](Node[S]):
    """Multi-branch switch node.

    Each entry in `cases` is a `(target, predicate)` pair. Cases are
    evaluated in `dict` insertion order; the first matching predicate's
    target receives a deliver. If no case matches, `default` is used.

    Example::

        g.add_node("switch", SwitchNode(
            cases={"a": lambda s: s.x < 5, "b": lambda s: s.x < 10},
            default="c",
        ))
        g.add_edge("switch", "a")
        g.add_edge("switch", "b")
        g.add_edge("switch", "c")
    """

    def __init__(
        self,
        cases: dict[str, Callable[[S], bool]],
        default: str,
    ) -> None:
        self.cases = cases
        self.default = default

    def execute(self, ctx: GraphContext[S], integrated_input: IntegratedInput) -> NodeResult:
        for target, predicate in self.cases.items():
            if predicate(ctx.state):
                self.deliver(None, target, ctx)
                return NodeResult()
        self.deliver(None, self.default, ctx)
        return NodeResult()


def build_conditional_graph[S: GraphState](
    predicate: Callable[[S], str],
    high_branch: Node[S],
    low_branch: Node[S],
    merge: Node[S],
) -> Graph[S]:
    """Build an if/else + merge topology.

    Topology::

        START -> conditional -> (high | low) -> merge -> END

    The `predicate` must return ``"high"`` or ``"low"``; the returned value
    is the deliver target. Both branches converge on `merge` via default
    edges, demonstrating that deliver-based branches can merge into a
    common downstream node.

    Returns the uncompiled `Graph[S]` — call `.compile()` then pass to
    `GraphEngine` to execute.
    """

    g: Graph[S] = Graph()
    g.add_node("conditional", ConditionalNode(predicate))
    g.add_node("high", high_branch)
    g.add_node("low", low_branch)
    g.add_node("merge", merge)
    g.add_edge(GraphNode.START, "conditional")
    g.add_edge("conditional", "high")
    g.add_edge("conditional", "low")
    g.add_edge("high", "merge")
    g.add_edge("low", "merge")
    g.add_edge("merge", GraphNode.END)
    return g


__all__ = [
    "ConditionalNode",
    "SwitchNode",
    "build_conditional_graph",
]
