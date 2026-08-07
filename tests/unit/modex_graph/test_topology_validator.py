"""Tests for `TopologyValidator` + `TopologyError` (ticket 08)."""

from __future__ import annotations

import pytest

from modex_graph import (
    EdgeSpec,
    GraphNode,
    GraphSpec,
    NodeSpec,
    TopologyError,
    TopologyValidator,
)


def _node(name: str) -> NodeSpec:
    return NodeSpec(name=name, node_type="function")


def _spec(
    nodes: list[NodeSpec],
    edges: list[EdgeSpec],
    *,
    max_iterations: int = 25,
) -> GraphSpec:
    return GraphSpec(
        name="test_graph",
        nodes=nodes,
        edges=edges,
        state_class="counter_state",
        max_iterations=max_iterations,
    )


def _raw_spec(
    nodes: list[NodeSpec],
    edges: list[EdgeSpec],
    *,
    max_iterations: int = 25,
) -> GraphSpec:
    """Build a GraphSpec bypassing Pydantic validation.

    Used to test TopologyValidator's self-contained checks on shapes that
    GraphSpec._validate_structure would reject at construction time.
    """
    return GraphSpec.model_construct(
        name="test_graph",
        nodes=nodes,
        edges=edges,
        state_class="counter_state",
        max_iterations=max_iterations,
    )


class TestTopologyError:
    def test_is_exception_subclass(self) -> None:
        assert issubclass(TopologyError, Exception)

    def test_carries_message(self) -> None:
        err = TopologyError("boom")
        assert str(err) == "boom"


class TestValidGraph:
    def test_minimal_linear_graph_passes(self) -> None:
        spec = _spec(
            nodes=[_node("n1")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="n1"),
                EdgeSpec(source="n1", target=GraphNode.END),
            ],
        )
        TopologyValidator().validate(spec)

    def test_multi_node_chain_passes(self) -> None:
        spec = _spec(
            nodes=[_node("a"), _node("b"), _node("c")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target="b"),
                EdgeSpec(source="b", target="c"),
                EdgeSpec(source="c", target=GraphNode.END),
            ],
        )
        TopologyValidator().validate(spec)

    def test_branching_graph_passes(self) -> None:
        spec = _spec(
            nodes=[_node("start"), _node("left"), _node("right"), _node("merge")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="start"),
                EdgeSpec(source="start", target="left"),
                EdgeSpec(source="start", target="right"),
                EdgeSpec(source="left", target="merge"),
                EdgeSpec(source="right", target="merge"),
                EdgeSpec(source="merge", target=GraphNode.END),
            ],
        )
        TopologyValidator().validate(spec)

    def test_react_cycle_passes(self) -> None:
        spec = _spec(
            nodes=[_node("llm"), _node("tool")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="llm"),
                EdgeSpec(source="llm", target="tool"),
                EdgeSpec(source="tool", target="llm"),
                EdgeSpec(source="llm", target=GraphNode.END),
            ],
        )
        TopologyValidator().validate(spec)


class TestEntryEdgeChecks:
    def test_missing_entry_edge_fails(self) -> None:
        raw = _raw_spec(
            nodes=[_node("n1")],
            edges=[EdgeSpec(source="n1", target=GraphNode.END)],
        )
        with pytest.raises(TopologyError, match="no entry edge"):
            TopologyValidator().validate(raw)

    def test_multiple_entry_edges_fails(self) -> None:
        spec = _spec(
            nodes=[_node("a"), _node("b")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source=GraphNode.START, target="b"),
                EdgeSpec(source="a", target=GraphNode.END),
                EdgeSpec(source="b", target=GraphNode.END),
            ],
        )
        with pytest.raises(TopologyError, match="multiple entry edges"):
            TopologyValidator().validate(spec)

    def test_entry_to_end_fails(self) -> None:
        raw = _raw_spec(
            nodes=[_node("n1")],
            edges=[
                EdgeSpec(source=GraphNode.START, target=GraphNode.END),
                EdgeSpec(source="n1", target=GraphNode.END),
            ],
        )
        with pytest.raises(TopologyError, match="cannot be"):
            TopologyValidator().validate(raw)


class TestExitEdgeCheck:
    def test_missing_exit_edge_fails(self) -> None:
        raw = _raw_spec(
            nodes=[_node("n1")],
            edges=[EdgeSpec(source=GraphNode.START, target="n1")],
        )
        with pytest.raises(TopologyError, match="no exit edge"):
            TopologyValidator().validate(raw)


class TestReachability:
    def test_unreachable_node_fails(self) -> None:
        raw = _raw_spec(
            nodes=[_node("entry"), _node("orphan")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target=GraphNode.END),
                EdgeSpec(source="orphan", target=GraphNode.END),
            ],
        )
        with pytest.raises(TopologyError, match="unreachable from entry"):
            TopologyValidator().validate(raw)

    def test_dead_end_node_fails(self) -> None:
        raw = _raw_spec(
            nodes=[_node("entry"), _node("dead_end")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target="dead_end"),
                EdgeSpec(source="entry", target=GraphNode.END),
            ],
        )
        with pytest.raises(TopologyError, match="cannot reach"):
            TopologyValidator().validate(raw)


class TestDuplicateEdges:
    def test_duplicate_edge_fails(self) -> None:
        raw = _raw_spec(
            nodes=[_node("a"), _node("b")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target="b"),
                EdgeSpec(source="a", target="b"),
                EdgeSpec(source="b", target=GraphNode.END),
            ],
        )
        with pytest.raises(TopologyError, match="Duplicate edge"):
            TopologyValidator().validate(raw)

    def test_same_pair_different_direction_is_not_duplicate(self) -> None:
        spec = _spec(
            nodes=[_node("llm"), _node("tool")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="llm"),
                EdgeSpec(source="llm", target="tool"),
                EdgeSpec(source="tool", target="llm"),
                EdgeSpec(source="llm", target=GraphNode.END),
            ],
        )
        TopologyValidator().validate(spec)


class TestSelfLoops:
    def test_self_loop_on_real_node_fails(self) -> None:
        raw = _raw_spec(
            nodes=[_node("n1")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="n1"),
                EdgeSpec(source="n1", target="n1"),
                EdgeSpec(source="n1", target=GraphNode.END),
            ],
        )
        with pytest.raises(TopologyError, match="Self-loop"):
            TopologyValidator().validate(raw)


class TestMaxNodes:
    def test_max_nodes_not_exceeded_passes(self) -> None:
        spec = _spec(
            nodes=[_node("a"), _node("b")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target="b"),
                EdgeSpec(source="b", target=GraphNode.END),
            ],
        )
        TopologyValidator().validate(spec, max_nodes=5)

    def test_max_nodes_exceeded_fails(self) -> None:
        spec = _spec(
            nodes=[_node("a"), _node("b"), _node("c")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target="b"),
                EdgeSpec(source="b", target="c"),
                EdgeSpec(source="c", target=GraphNode.END),
            ],
        )
        with pytest.raises(TopologyError, match="exceeding max_nodes"):
            TopologyValidator().validate(spec, max_nodes=2)

    def test_max_nodes_exact_boundary_passes(self) -> None:
        spec = _spec(
            nodes=[_node("a"), _node("b")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target="b"),
                EdgeSpec(source="b", target=GraphNode.END),
            ],
        )
        TopologyValidator().validate(spec, max_nodes=2)


class TestMaxDepth:
    def test_depth_within_limit_passes(self) -> None:
        spec = _spec(
            nodes=[_node("a"), _node("b"), _node("c")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target="b"),
                EdgeSpec(source="b", target="c"),
                EdgeSpec(source="c", target=GraphNode.END),
            ],
        )
        # Path START→a→b→c→END = 4 edges.
        TopologyValidator().validate(spec, max_depth=4)

    def test_depth_exceeded_fails(self) -> None:
        spec = _spec(
            nodes=[_node("a"), _node("b"), _node("c")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target="b"),
                EdgeSpec(source="b", target="c"),
                EdgeSpec(source="c", target=GraphNode.END),
            ],
        )
        with pytest.raises(TopologyError, match="exceeding max_depth"):
            TopologyValidator().validate(spec, max_depth=3)

    def test_depth_picks_longest_branch(self) -> None:
        spec = _spec(
            nodes=[_node("start"), _node("short"), _node("long1"), _node("long2")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="start"),
                EdgeSpec(source="start", target="short"),
                EdgeSpec(source="start", target="long1"),
                EdgeSpec(source="long1", target="long2"),
                EdgeSpec(source="short", target=GraphNode.END),
                EdgeSpec(source="long2", target=GraphNode.END),
            ],
        )
        # Longest path: START→start→long1→long2→END = 4 edges.
        # Short branch: START→start→short→END = 3 edges.
        with pytest.raises(TopologyError, match="exceeding max_depth"):
            TopologyValidator().validate(spec, max_depth=3)
        TopologyValidator().validate(spec, max_depth=4)

    def test_depth_with_cycle_uses_simple_path(self) -> None:
        """max_depth counts the longest simple path (no repeated nodes)."""
        spec = _spec(
            nodes=[_node("llm"), _node("tool")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="llm"),
                EdgeSpec(source="llm", target="tool"),
                EdgeSpec(source="tool", target="llm"),
                EdgeSpec(source="llm", target=GraphNode.END),
            ],
        )
        # Simple paths: START→llm→END (2), START→llm→tool→llm→END is NOT
        # simple (llm repeats). Longest simple: START→llm→tool→END? No —
        # there's no tool→END edge. So the only simple path to END is
        # START→llm→END (2 edges).
        TopologyValidator().validate(spec, max_depth=2)


class TestCyclesAllowed:
    def test_two_node_cycle_passes(self) -> None:
        spec = _spec(
            nodes=[_node("llm"), _node("tool")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="llm"),
                EdgeSpec(source="llm", target="tool"),
                EdgeSpec(source="tool", target="llm"),
                EdgeSpec(source="llm", target=GraphNode.END),
            ],
        )
        TopologyValidator().validate(spec)

    def test_three_node_cycle_passes(self) -> None:
        spec = _spec(
            nodes=[_node("a"), _node("b"), _node("c")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target="b"),
                EdgeSpec(source="b", target="c"),
                EdgeSpec(source="c", target="a"),
                EdgeSpec(source="c", target=GraphNode.END),
            ],
        )
        TopologyValidator().validate(spec)


class TestMaxIterationsRecheck:
    def test_zero_max_iterations_fails(self) -> None:
        raw = _raw_spec(
            nodes=[_node("n1")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="n1"),
                EdgeSpec(source="n1", target=GraphNode.END),
            ],
            max_iterations=0,
        )
        with pytest.raises(TopologyError, match="max_iterations must be > 0"):
            TopologyValidator().validate(raw)

    def test_negative_max_iterations_fails(self) -> None:
        raw = _raw_spec(
            nodes=[_node("n1")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="n1"),
                EdgeSpec(source="n1", target=GraphNode.END),
            ],
            max_iterations=-1,
        )
        with pytest.raises(TopologyError, match="max_iterations must be > 0"):
            TopologyValidator().validate(raw)
