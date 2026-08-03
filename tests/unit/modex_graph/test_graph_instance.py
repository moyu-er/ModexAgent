"""Tests for `GraphInstance` — runtime graph instance abstraction (ticket 04).

Covers:

- `GraphInstance` is a Pydantic `BaseModel`, frozen, `extra="forbid"`.
- Required fields (`graph_instance_id`, `spec_id`) must be provided.
- Default values (`parent_instance_id=None`, `parent_node=None`,
  `status="running"`).
- Parent linkage (nested subgraph) via `parent_instance_id` + `parent_node`.
- Serialization round-trip (`model_dump` / `model_validate` and
  `model_dump_json` / `model_validate_json`).
- `graph_instance_id` is `int` (Snowflake), not `str`.
- Frozen immutability + extra-field rejection.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from modex_graph import GraphInstance


class TestGraphInstanceModel:
    def test_is_pydantic_model(self) -> None:
        assert issubclass(GraphInstance, BaseModel)

    def test_frozen(self) -> None:
        assert GraphInstance.model_config.get("frozen") is True

    def test_extra_forbid(self) -> None:
        assert GraphInstance.model_config.get("extra") == "forbid"


class TestGraphInstanceConstruction:
    def test_minimal_outer_instance(self) -> None:
        """Minimal construction: only the two required IDs. Defaults apply."""
        inst = GraphInstance(graph_instance_id=123, spec_id=456)
        assert inst.graph_instance_id == 123
        assert inst.spec_id == 456
        assert inst.parent_instance_id is None
        assert inst.parent_node is None
        assert inst.status == "running"

    def test_graph_instance_id_is_int_not_str(self) -> None:
        """graph_instance_id is a Snowflake int (matches BIGINT in P0.2 DDL),
        not a str — even though the PRD text says str."""
        inst = GraphInstance(graph_instance_id=1, spec_id=2)
        assert isinstance(inst.graph_instance_id, int)
        assert not isinstance(inst.graph_instance_id, str)

    def test_default_status_is_running(self) -> None:
        inst = GraphInstance(graph_instance_id=1, spec_id=2)
        assert inst.status == "running"

    def test_explicit_status(self) -> None:
        inst = GraphInstance(
            graph_instance_id=1,
            spec_id=2,
            status="paused",
        )
        assert inst.status == "paused"

    def test_required_fields_missing_raises(self) -> None:
        with pytest.raises(ValidationError):
            GraphInstance(spec_id=456)  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            GraphInstance(graph_instance_id=123)  # type: ignore[call-arg]

    def test_non_numeric_string_graph_instance_id_rejected(self) -> None:
        """Non-numeric strings are rejected by Pydantic int validation."""
        with pytest.raises(ValidationError):
            GraphInstance(graph_instance_id="abc", spec_id=456)  # type: ignore[arg-type]

    def test_numeric_string_graph_instance_id_coerced(self) -> None:
        """Pydantic v2 default lenient mode coerces numeric strings to int.
        This is the expected behavior for JSON/DB round-trips where int
        values may arrive as strings. The stored value is int."""
        inst = GraphInstance(graph_instance_id="123", spec_id=456)  # type: ignore[arg-type]
        assert inst.graph_instance_id == 123
        assert isinstance(inst.graph_instance_id, int)


class TestGraphInstanceParentLinkage:
    def test_nested_subgraph_with_parent(self) -> None:
        """A nested subgraph instance carries parent linkage."""
        parent = GraphInstance(graph_instance_id=100, spec_id=200)
        child = GraphInstance(
            graph_instance_id=101,
            spec_id=201,
            parent_instance_id=parent.graph_instance_id,
            parent_node="subgraph_node",
        )
        assert child.parent_instance_id == 100
        assert child.parent_node == "subgraph_node"
        assert parent.parent_instance_id is None
        assert parent.parent_node is None

    def test_parent_instance_id_only(self) -> None:
        """parent_instance_id can be set without parent_node."""
        inst = GraphInstance(
            graph_instance_id=1,
            spec_id=2,
            parent_instance_id=99,
        )
        assert inst.parent_instance_id == 99
        assert inst.parent_node is None

    def test_parent_node_only(self) -> None:
        """parent_node can be set without parent_instance_id (unusual but allowed)."""
        inst = GraphInstance(
            graph_instance_id=1,
            spec_id=2,
            parent_node="creator_node",
        )
        assert inst.parent_instance_id is None
        assert inst.parent_node == "creator_node"

    def test_multi_level_nesting(self) -> None:
        """Recursive nesting: grandchild links to child links to parent."""
        parent = GraphInstance(graph_instance_id=1, spec_id=10)
        child = GraphInstance(
            graph_instance_id=2,
            spec_id=10,
            parent_instance_id=1,
            parent_node="child_spawner",
        )
        grandchild = GraphInstance(
            graph_instance_id=3,
            spec_id=10,
            parent_instance_id=2,
            parent_node="grandchild_spawner",
        )
        assert grandchild.parent_instance_id == child.graph_instance_id
        assert child.parent_instance_id == parent.graph_instance_id
        assert parent.parent_instance_id is None


class TestGraphInstanceFrozen:
    def test_frozen_immutable(self) -> None:
        inst = GraphInstance(graph_instance_id=1, spec_id=2)
        with pytest.raises(ValidationError):
            inst.graph_instance_id = 999  # type: ignore[misc]
        with pytest.raises(ValidationError):
            inst.status = "completed"  # type: ignore[misc]

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GraphInstance(  # type: ignore[call-arg]
                graph_instance_id=1,
                spec_id=2,
                bogus_field="bad",
            )


class TestGraphInstanceSerialization:
    def test_model_dump_round_trip(self) -> None:
        original = GraphInstance(
            graph_instance_id=123456789,
            spec_id=42,
            parent_instance_id=999,
            parent_node="spawner",
            status="paused",
        )
        restored = GraphInstance.model_validate(original.model_dump())
        assert restored == original

    def test_model_dump_json_round_trip(self) -> None:
        original = GraphInstance(
            graph_instance_id=123456789,
            spec_id=42,
            parent_instance_id=999,
            parent_node="spawner",
            status="paused",
        )
        json_str = original.model_dump_json()
        restored = GraphInstance.model_validate_json(json_str)
        assert restored == original

    def test_round_trip_preserves_none_parents(self) -> None:
        """Outer instance (no parent) round-trips with None parents."""
        original = GraphInstance(graph_instance_id=1, spec_id=2)
        restored = GraphInstance.model_validate(original.model_dump())
        assert restored == original
        assert restored.parent_instance_id is None
        assert restored.parent_node is None

    def test_round_trip_preserves_default_status(self) -> None:
        original = GraphInstance(graph_instance_id=1, spec_id=2)
        restored = GraphInstance.model_validate_json(original.model_dump_json())
        assert restored.status == "running"
        assert restored == original

    def test_model_dump_contains_all_fields(self) -> None:
        inst = GraphInstance(graph_instance_id=1, spec_id=2)
        dumped = inst.model_dump()
        assert set(dumped.keys()) == {
            "graph_instance_id",
            "spec_id",
            "parent_instance_id",
            "parent_node",
            "status",
        }


class TestGraphInstanceIsExported:
    def test_importable_from_modex_graph(self) -> None:
        from modex_graph import GraphInstance as Direct

        assert Direct is GraphInstance

    def test_in_all(self) -> None:
        import modex_graph

        assert "GraphInstance" in modex_graph.__all__
