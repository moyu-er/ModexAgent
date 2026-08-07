"""Public surface import test — all acceptance-criteria exports resolve."""

from __future__ import annotations


def test_all_exports_importable() -> None:
    import modex_graph

    expected = {
        "Graph",
        "Node",
        "CompiledGraph",
        "GraphEngine",
        "GraphContext",
        "GraphState",
        "GraphRuntime",
        "GraphBubbleUp",
        "GraphInterrupt",
        "GraphDrained",
        "ParentCommand",
        "GraphNode",
        "RoutingError",
        "GraphRecursionError",
    }
    actual = set(modex_graph.__all__)
    missing = expected - actual
    assert not missing, f"Missing exports: {missing}"


def test_graph_node_sentinels() -> None:
    from modex_graph import GraphNode

    assert GraphNode.START == "__start__"
    assert GraphNode.END == "__end__"
    assert isinstance(GraphNode.START, str)
    assert isinstance(GraphNode.END, str)


def test_node_abc_has_async_execute() -> None:
    import inspect

    from modex_graph import Node

    execute = Node.execute
    assert inspect.iscoroutinefunction(execute)


def test_compiled_graph_is_node_subclass() -> None:
    from modex_graph import CompiledGraph, Node

    assert issubclass(CompiledGraph, Node), (
        "CompiledGraph must be a subclass of Node (Graph-is-a-Node, ADR-0033 D8)."
    )
