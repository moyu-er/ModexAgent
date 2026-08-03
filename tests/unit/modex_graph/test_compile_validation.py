"""compile() validation: dangling edge / missing entry / duplicate name / cycle warn."""

from __future__ import annotations

import warnings

import pytest
from helpers import CounterState

from modex_graph import Graph, GraphNode, Node, NodeResult, RoutingError


class _NoOpNode(Node[CounterState]):
    def execute(self, ctx, integrated_input):  # type: ignore[no-untyped-def]
        return NodeResult()


class TestCompileValidation:
    def test_missing_entry_raises(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", _NoOpNode())
        g.add_edge("a", GraphNode.END)
        with pytest.raises(RoutingError, match="entry node"):
            g.compile()

    def test_multiple_entries_raises(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", _NoOpNode())
        g.add_node("b", _NoOpNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge(GraphNode.START, "b")
        with pytest.raises(RoutingError, match="multiple entry"):
            g.compile()

    def test_dangling_edge_target_raises(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", _NoOpNode())
        g.add_edge(GraphNode.START, "a")
        # "b" is not a registered node
        g.add_edge("a", "b")
        with pytest.raises(RoutingError, match="not a registered node"):
            g.compile()

    def test_dangling_edge_source_raises(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", _NoOpNode())
        g.add_edge(GraphNode.START, "a")
        # "b" source doesn't exist
        g.add_edge("b", "a")
        with pytest.raises(RoutingError, match="not a registered node"):
            g.compile()

    def test_duplicate_node_name_overwrites(self) -> None:
        """add_node uses a dict, so registering the same name twice overwrites."""
        g: Graph[CounterState] = Graph()
        g.add_node("a", _NoOpNode())
        g.add_node("a", _NoOpNode())  # overwrites
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile()
        assert "a" in compiled.nodes
        # Only one entry for "a" (dict semantics).
        assert len(compiled.nodes) == 1

    def test_cycle_warn_does_not_raise(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", _NoOpNode())
        g.add_node("b", _NoOpNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", "a")  # back-edge → cycle
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            compiled = g.compile(cycle_detection="warn")
            assert any("cycle" in str(warning.message).lower() for warning in w)
        assert compiled.entry_node == "a"

    def test_cycle_raise_raises(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", _NoOpNode())
        g.add_node("b", _NoOpNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", "a")
        with pytest.raises(RoutingError, match="cycle"):
            g.compile(cycle_detection="raise")

    def test_cycle_off_no_warning(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", _NoOpNode())
        g.add_node("b", _NoOpNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", "a")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            g.compile(cycle_detection="off")
            assert not any("cycle" in str(warning.message).lower() for warning in w)

    def test_valid_graph_compiles(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", _NoOpNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile(max_iterations=50)
        assert compiled.entry_node == "a"
        assert compiled.max_iterations == 50

    def test_max_iterations_default_100(self) -> None:
        g: Graph[CounterState] = Graph()
        g.add_node("a", _NoOpNode())
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile()
        assert compiled.max_iterations == 100
