"""Tests for `GraphSpec` + `NodeSpec` + `EdgeSpec` — declarative graph spec."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_graph import (
    EdgeSpec,
    GraphNode,
    GraphSpec,
    NodeSpec,
    NodeTrigger,
    SchedulerKind,
)


class TestNodeSpec:
    """`NodeSpec` — declarative node specification."""

    def test_minimal(self) -> None:
        spec = NodeSpec(name="n1", node_type="function")
        assert spec.name == "n1"
        assert spec.node_type == "function"
        assert spec.config == {}
        assert spec.trigger is None

    def test_with_config_and_trigger(self) -> None:
        spec = NodeSpec(
            name="llm",
            node_type="agent",
            config={"model": "gpt-4", "temperature": 0.7},
            trigger=NodeTrigger.ON_RECEIVE,
        )
        assert spec.config == {"model": "gpt-4", "temperature": 0.7}
        assert spec.trigger == NodeTrigger.ON_RECEIVE

    def test_frozen(self) -> None:
        spec = NodeSpec(name="n1", node_type="function")
        with pytest.raises(ValidationError):
            spec.name = "n2"  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            NodeSpec(name="n1", node_type="function", bogus=True)  # type: ignore[call-arg]

    def test_serialization_round_trip(self) -> None:
        spec = NodeSpec(name="n1", node_type="function", config={"x": 1})
        restored = NodeSpec.model_validate(spec.model_dump())
        assert restored == spec


class TestEdgeSpec:
    """`EdgeSpec` — topology only, NO `reason` field (ticket 07 correction)."""

    def test_basic(self) -> None:
        edge = EdgeSpec(source="n1", target="n2")
        assert edge.source == "n1"
        assert edge.target == "n2"

    def test_with_sentinels(self) -> None:
        edge = EdgeSpec(source=GraphNode.START, target="entry")
        assert edge.source == GraphNode.START
        assert edge.target == "entry"

        edge2 = EdgeSpec(source="terminal", target=GraphNode.END)
        assert edge2.target == GraphNode.END

    def test_no_reason_field(self) -> None:
        """EdgeSpec must NOT have a `reason` field (ticket 07 deliver/submit model)."""
        edge = EdgeSpec(source="n1", target="n2")
        assert not hasattr(edge, "reason")

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            EdgeSpec(source="n1", target="n2", reason="begin")  # type: ignore[call-arg]

    def test_frozen(self) -> None:
        edge = EdgeSpec(source="n1", target="n2")
        with pytest.raises(ValidationError):
            edge.source = "n3"  # type: ignore[misc]

    def test_serialization_round_trip(self) -> None:
        edge = EdgeSpec(source=GraphNode.START, target="n1")
        restored = EdgeSpec.model_validate(edge.model_dump())
        assert restored == edge


class TestGraphSpec:
    """`GraphSpec` — declarative graph specification with structural validation."""

    def test_minimal_valid(self) -> None:
        spec = GraphSpec(
            name="g1",
            nodes=[NodeSpec(name="entry", node_type="function")],
            edges=[EdgeSpec(source=GraphNode.START, target="entry")],
            state_class="counter_state",
        )
        assert spec.name == "g1"
        assert len(spec.nodes) == 1
        assert len(spec.edges) == 1
        assert spec.scheduler == SchedulerKind.LINEAR
        assert spec.version == "1.0"
        assert spec.max_iterations == 25
        assert spec.default_trigger == NodeTrigger.ON_ALL_PREDS

    def test_state_class_registry_name(self) -> None:
        spec = GraphSpec(
            name="g1",
            nodes=[NodeSpec(name="entry", node_type="function")],
            edges=[EdgeSpec(source=GraphNode.START, target="entry")],
            state_class="my_registered_state",
        )
        assert spec.state_class == "my_registered_state"

    def test_frozen(self) -> None:
        spec = GraphSpec(
            name="g1",
            nodes=[NodeSpec(name="entry", node_type="function")],
            edges=[EdgeSpec(source=GraphNode.START, target="entry")],
            state_class="counter_state",
        )
        with pytest.raises(ValidationError):
            spec.name = "g2"  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            GraphSpec(
                name="g1",
                nodes=[NodeSpec(name="entry", node_type="function")],
                edges=[EdgeSpec(source=GraphNode.START, target="entry")],
                state_class="counter_state",
                bogus=True,  # type: ignore[call-arg]
            )

    def test_serialization_round_trip(self) -> None:
        spec = GraphSpec(
            name="g1",
            nodes=[NodeSpec(name="entry", node_type="function", config={"x": 1})],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target=GraphNode.END),
            ],
            state_class="counter_state",
            scheduler=SchedulerKind.PARALLEL,
            max_iterations=50,
        )
        data = spec.model_dump()
        restored = GraphSpec.model_validate(data)
        assert restored == spec

    def test_json_serialization_round_trip(self) -> None:
        """GraphSpec must be JSON-serializable for persistence."""
        spec = GraphSpec(
            name="g1",
            nodes=[NodeSpec(name="entry", node_type="function")],
            edges=[EdgeSpec(source=GraphNode.START, target="entry")],
            state_class="counter_state",
        )
        json_str = spec.model_dump_json()
        restored = GraphSpec.model_validate_json(json_str)
        assert restored == spec

    # ── Structural validation ──────────────────────────────────────────

    def test_validation_empty_nodes_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            GraphSpec(
                name="g1",
                nodes=[],
                edges=[EdgeSpec(source=GraphNode.START, target="entry")],
                state_class="counter_state",
            )
        assert "at least one node" in str(exc_info.value)

    def test_validation_duplicate_node_names_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            GraphSpec(
                name="g1",
                nodes=[
                    NodeSpec(name="dup", node_type="function"),
                    NodeSpec(name="dup", node_type="function"),
                ],
                edges=[EdgeSpec(source=GraphNode.START, target="dup")],
                state_class="counter_state",
            )
        assert "Duplicate node names" in str(exc_info.value)

    def test_validation_no_entry_edge_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            GraphSpec(
                name="g1",
                nodes=[NodeSpec(name="entry", node_type="function")],
                edges=[EdgeSpec(source="entry", target=GraphNode.END)],
                state_class="counter_state",
            )
        assert "entry edge" in str(exc_info.value)

    def test_validation_zero_max_iterations_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            GraphSpec(
                name="g1",
                nodes=[NodeSpec(name="entry", node_type="function")],
                edges=[EdgeSpec(source=GraphNode.START, target="entry")],
                state_class="counter_state",
                max_iterations=0,
            )
        assert "max_iterations" in str(exc_info.value)

    def test_validation_edge_unknown_endpoint_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            GraphSpec(
                name="g1",
                nodes=[NodeSpec(name="entry", node_type="function")],
                edges=[
                    EdgeSpec(source=GraphNode.START, target="entry"),
                    EdgeSpec(source="entry", target="nonexistent"),
                ],
                state_class="counter_state",
            )
        assert "unknown node" in str(exc_info.value)

    def test_validation_sentinel_endpoints_allowed(self) -> None:
        """Edges to GraphNode.END and from GraphNode.START are valid."""
        spec = GraphSpec(
            name="g1",
            nodes=[NodeSpec(name="n1", node_type="function")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="n1"),
                EdgeSpec(source="n1", target=GraphNode.END),
            ],
            state_class="counter_state",
        )
        assert len(spec.edges) == 2

    def test_multi_node_graph_valid(self) -> None:
        spec = GraphSpec(
            name="pipeline",
            nodes=[
                NodeSpec(name="start", node_type="function"),
                NodeSpec(name="middle", node_type="function"),
                NodeSpec(name="end", node_type="function"),
            ],
            edges=[
                EdgeSpec(source=GraphNode.START, target="start"),
                EdgeSpec(source="start", target="middle"),
                EdgeSpec(source="middle", target="end"),
                EdgeSpec(source="end", target=GraphNode.END),
            ],
            state_class="counter_state",
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_RECEIVE,
        )
        assert len(spec.nodes) == 3
        assert len(spec.edges) == 4
