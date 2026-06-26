"""Tests for Edge and Graph."""
import pytest
from modex_agent.core.graph.graph import Edge, Graph
from modex_agent.core.graph.node import Node, NodeTransition
from modex_agent.core.graph.constants import GraphNode


class _NoOpNode(Node):
    def __init__(self, name: str):
        super().__init__(name)

    async def execute(self, ctx):
        return NodeTransition(GraphNode.END, "done")


class TestEdge:
    def test_unconditional_edge(self):
        e = Edge("a", "b")
        assert e.source == "a"
        assert e.target == "b"
        assert e.reason is None

    def test_conditional_edge(self):
        e = Edge("a", "b", reason="has_tools")
        assert e.reason == "has_tools"

    def test_frozen(self):
        e = Edge("a", "b")
        with pytest.raises(Exception):
            e.target = "c"


class TestGraph:
    def test_add_node(self):
        g = Graph()
        g.add_node(_NoOpNode("n1"))
        assert "n1" in g._nodes

    def test_add_edge(self):
        g = Graph()
        g.add_edge("a", "b", reason="r1")
        assert len(g._edges["a"]) == 1
        assert g._edges["a"][0].target == "b"

    def test_next_node_exact_match(self):
        g = Graph()
        g.add_edge("a", "b", reason="r1")
        g.add_edge("a", "c", reason="r2")
        assert g.next_node("a", "r1") == "b"
        assert g.next_node("a", "r2") == "c"

    def test_next_node_fallback_to_unconditional(self):
        g = Graph()
        g.add_edge("a", "b", reason="r1")
        g.add_edge("a", "c", reason=None)
        assert g.next_node("a", "unknown_reason") == "c"

    def test_next_node_no_match_raises(self):
        g = Graph()
        g.add_edge("a", "b", reason="r1")
        with pytest.raises(KeyError):
            g.next_node("a", "no_such_reason")

    def test_entry_node_default(self):
        g = Graph()
        assert g.entry_node == "start"

    def test_get_node_returns_added_node(self):
        g = Graph()
        node = _NoOpNode("n1")
        g.add_node(node)
        assert g.get_node("n1") is node

    def test_get_node_missing_raises_key_error(self):
        g = Graph()
        with pytest.raises(KeyError):
            g.get_node("nope")
