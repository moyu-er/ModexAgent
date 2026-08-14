"""Tests for `GraphInstance` — runtime graph instance.

Covers the evolution from frozen Pydantic data record to a
runtime class holding ``coordinator + GraphMetadata``:

- Construction: ``GraphInstance(metadata, coordinator)``.
- Property delegation: ``graph_instance_id`` / ``spec_id`` /
  ``parent_instance_id`` / ``parent_node`` / ``status`` delegate to
  ``metadata``.
- Method delegation: ``get_state()`` delegates to ``coordinator``.
- ``metadata`` is the serializable value object (frozen Pydantic).
- ``coordinator`` lifecycle is bound to the ``GraphInstance`` lifecycle.
- Status transitions are routed directly through ``GraphInstanceStore`` by
  the orchestrator / recovery / control services — ``GraphInstance`` no
  longer owns a status-update method.
- Export from ``modex_graph``.
"""

from __future__ import annotations

import pytest
from helpers import make_graph_instance, make_graph_metadata
from pydantic import BaseModel

from modex_graph import (
    GraphInstance,
    GraphInstanceStatus,
    GraphMetadata,
    GraphStateSnapshot,
    create_null_coordinator,
)


class TestGraphInstanceConstruction:
    def test_requires_metadata_and_coordinator(self) -> None:
        """GraphInstance is constructed with metadata + coordinator."""
        metadata = make_graph_metadata(gid=123, spec_id=456)
        coordinator = create_null_coordinator(123)
        instance = GraphInstance(metadata, coordinator)
        assert instance.metadata is metadata
        assert instance.coordinator is coordinator

    def test_metadata_is_graph_metadata(self) -> None:
        """The metadata attribute is a GraphMetadata (frozen Pydantic)."""
        instance = make_graph_instance(gid=1, spec_id=2)
        assert isinstance(instance.metadata, GraphMetadata)
        assert isinstance(instance.metadata, BaseModel)

    def test_coordinator_is_persistence_coordinator(self) -> None:
        """The coordinator attribute is a GraphPersistenceCoordinator."""
        from modex_graph import GraphPersistenceCoordinator

        instance = make_graph_instance(gid=1)
        assert isinstance(instance.coordinator, GraphPersistenceCoordinator)

    def test_metadata_is_frozen(self) -> None:
        """GraphMetadata (the value object) is still frozen Pydantic."""
        assert GraphMetadata.model_config.get("frozen") is True

    def test_graph_instance_is_not_pydantic_model(self) -> None:
        """GraphInstance is a plain class, NOT a Pydantic BaseModel."""
        assert not issubclass(GraphInstance, BaseModel)


class TestGraphInstancePropertyDelegation:
    def test_graph_instance_id_delegates_to_metadata(self) -> None:
        instance = make_graph_instance(gid=123, spec_id=456)
        assert instance.graph_instance_id == 123
        assert instance.graph_instance_id == instance.metadata.graph_instance_id

    def test_spec_id_delegates_to_metadata(self) -> None:
        instance = make_graph_instance(gid=1, spec_id=789)
        assert instance.spec_id == 789
        assert instance.spec_id == instance.metadata.spec_id

    def test_parent_instance_id_delegates_to_metadata(self) -> None:
        instance = make_graph_instance(gid=1, parent_instance_id=99)
        assert instance.parent_instance_id == 99
        assert instance.parent_instance_id == instance.metadata.parent_instance_id

    def test_parent_instance_id_none_for_outer(self) -> None:
        instance = make_graph_instance(gid=1)
        assert instance.parent_instance_id is None

    def test_parent_node_delegates_to_metadata(self) -> None:
        instance = make_graph_instance(gid=1, parent_node="spawner")
        assert instance.parent_node == "spawner"
        assert instance.parent_node == instance.metadata.parent_node

    def test_parent_node_none_for_outer(self) -> None:
        instance = make_graph_instance(gid=1)
        assert instance.parent_node is None

    def test_status_delegates_to_metadata(self) -> None:
        instance = make_graph_instance(gid=1, status=GraphInstanceStatus.PAUSED)
        assert instance.status == GraphInstanceStatus.PAUSED
        assert instance.status == instance.metadata.status

    def test_status_default_is_running(self) -> None:
        instance = make_graph_instance(gid=1)
        assert instance.status == GraphInstanceStatus.RUNNING
        assert instance.status == "running"

    def test_graph_instance_id_is_int(self) -> None:
        """graph_instance_id is a Snowflake int (matches BIGINT in DDL)."""
        instance = make_graph_instance(gid=42, spec_id=2)
        assert isinstance(instance.graph_instance_id, int)
        assert not isinstance(instance.graph_instance_id, str)


class TestGraphInstanceParentLinkage:
    def test_nested_subgraph_with_parent(self) -> None:
        parent = make_graph_instance(gid=100, spec_id=200)
        child = make_graph_instance(
            gid=101, spec_id=201, parent_instance_id=100, parent_node="subgraph_node"
        )
        assert child.parent_instance_id == 100
        assert child.parent_node == "subgraph_node"
        assert parent.parent_instance_id is None
        assert parent.parent_node is None

    def test_multi_level_nesting(self) -> None:
        parent = make_graph_instance(gid=1, spec_id=10)
        child = make_graph_instance(
            gid=2, spec_id=10, parent_instance_id=1, parent_node="child_spawner"
        )
        grandchild = make_graph_instance(
            gid=3, spec_id=10, parent_instance_id=2, parent_node="grandchild_spawner"
        )
        assert grandchild.parent_instance_id == child.graph_instance_id
        assert child.parent_instance_id == parent.graph_instance_id
        assert parent.parent_instance_id is None


class TestGraphInstanceGetState:
    def test_get_state_returns_graph_state_snapshot(self) -> None:
        """get_state() delegates to coordinator.get_graph_state()."""
        instance = make_graph_instance(gid=42)
        state = instance.get_state()
        assert isinstance(state, GraphStateSnapshot)

    def test_get_state_metadata_has_correct_instance_id(self) -> None:
        """The snapshot's metadata carries the coordinator's graph_instance_id."""
        instance = make_graph_instance(gid=42)
        state = instance.get_state()
        assert state.metadata.graph_instance_id == 42

    def test_get_state_with_no_nodes(self) -> None:
        """A null coordinator with no registered nodes returns empty nodes dict."""
        instance = make_graph_instance(gid=1)
        state = instance.get_state()
        assert state.nodes == {}


class TestGraphInstanceNoUpdateStatusMethod:
    """GraphInstance no longer owns a status-update method.

    Status transitions are routed directly through ``GraphInstanceStore``
    (``store.update_status(gid, status)``) by the orchestrator / recovery /
    control services that hold the store. ``GraphInstance`` exposes
    ``status`` as a read-only property delegating to ``metadata``.
    """

    def test_update_status_method_removed(self) -> None:
        """No ``update_status`` attribute on GraphInstance."""
        instance = make_graph_instance(gid=1)
        assert not hasattr(instance, "update_status")

    def test_status_is_read_only_property(self) -> None:
        """``status`` is a read-only property — direct assignment raises."""
        instance = make_graph_instance(gid=1, status=GraphInstanceStatus.RUNNING)
        with pytest.raises(AttributeError):
            instance.status = GraphInstanceStatus.PAUSED  # type: ignore[misc]


class TestGraphInstanceIsExported:
    def test_importable_from_modex_graph(self) -> None:
        from modex_graph import GraphInstance as Direct

        assert Direct is GraphInstance

    def test_in_all(self) -> None:
        import modex_graph

        assert "GraphInstance" in modex_graph.__all__

