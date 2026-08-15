from __future__ import annotations

from abc import ABC
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from modex_graph import (
    DeliverConsumptionStatus,
    DeliverRecord,
    DeliverStore,
    DeliverStoreFactory,
    GraphInstanceStatus,
    GraphInstanceStore,
    GraphMetadata,
    GraphStateSnapshot,
    InMemoryNodeStateStore,
    InvocationContext,
    InvocationStatus,
    NodeInstanceStatus,
    NodeInvocationRecord,
    NodeStateStore,
    NullGraphInstanceStore,
    NullNodeStateStore,
)


def _node_record() -> NodeInvocationRecord:
    return NodeInvocationRecord(
        invocation_id=101,
        graph_instance_id=202,
        node_id="worker",
        version=0,
        parent_version=None,
        status=InvocationStatus.RUNNING,
        created_at=1_000,
        updated_at=1_001,
    )


def _metadata() -> GraphMetadata:
    return GraphMetadata(
        graph_instance_id=202,
        spec_id=303,
        parent_instance_id=None,
        parent_node=None,
        status=GraphInstanceStatus.RUNNING,
    )


def test_distributed_persistence_enums_have_the_specified_values() -> None:
    assert list(NodeInstanceStatus) == [
        NodeInstanceStatus.DORMANT,
        NodeInstanceStatus.PENDING,
        NodeInstanceStatus.READY,
        NodeInstanceStatus.RUNNING,
        NodeInstanceStatus.COMPLETED,
    ]
    assert list(InvocationStatus) == [
        InvocationStatus.RUNNING,
        InvocationStatus.COMPLETED,
        InvocationStatus.CANCELED,
        InvocationStatus.CRASHED,
    ]
    assert list(DeliverConsumptionStatus) == [
        DeliverConsumptionStatus.STAGED,
        DeliverConsumptionStatus.PENDING,
        DeliverConsumptionStatus.CONSUMED_PENDING,
        DeliverConsumptionStatus.CONSUMED_COMPLETED,
    ]


def test_node_invocation_record_is_frozen() -> None:
    record = _node_record()

    with pytest.raises(ValidationError):
        record.node_id = "other"
    with pytest.raises(ValidationError):
        NodeInvocationRecord.model_validate({**record.model_dump(), "extra": True})


def test_graph_persistence_value_objects_retain_history_data() -> None:
    record = _node_record()
    metadata = _metadata()

    context = InvocationContext(
        invocation_id=record.invocation_id,
        node_id=record.node_id,
        version=record.version,
        parent_version=record.parent_version,
    )
    snapshot = GraphStateSnapshot(metadata=metadata, nodes={record.node_id: [record]})

    assert context.invocation_id == record.invocation_id
    assert snapshot.nodes[record.node_id] == [record]
    for model in (
        NodeInvocationRecord,
        GraphMetadata,
        InvocationContext,
        GraphStateSnapshot,
    ):
        assert model.model_config.get("frozen") is True
        assert model.model_config.get("extra") == "forbid"


def test_persistence_interfaces_are_abstract_with_the_specified_methods() -> None:
    assert issubclass(NodeStateStore, ABC)
    assert issubclass(GraphInstanceStore, ABC)
    assert issubclass(DeliverStoreFactory, ABC)
    assert set(GraphInstanceStore.__abstractmethods__) == {
        "save",
        "load",
        "load_by_status",
        "load_by_parent",
        "update_attrs",
        "update_status",
        "delete",
        "begin_invocation",
        "complete_invocation",
        "suspend_invocation",
        "crash_invocation",
        "finalize_invocation",
    }
    assert set(DeliverStoreFactory.__abstractmethods__) == {"create"}

    with pytest.raises(TypeError):
        GraphInstanceStore()
    with pytest.raises(TypeError):
        NodeStateStore(0)  # type: ignore[abstract]
    with pytest.raises(TypeError):
        DeliverStoreFactory()


def test_null_graph_instance_store_is_concrete_no_op() -> None:
    store = NullGraphInstanceStore()
    assert len(NullGraphInstanceStore.__abstractmethods__) == 0
    assert store.load(0) is None
    assert store.load_by_status(GraphInstanceStatus.CRASHED) == []
    assert store.load_by_parent(0) == []
    store.save(_metadata())
    store.update_status(0, GraphInstanceStatus.PAUSED)
    store.delete(0)
    assert store.load(0) is None


def test_null_node_state_store_is_concrete_no_op() -> None:
    store = NullNodeStateStore(0)
    assert len(NullNodeStateStore.__abstractmethods__) == 0
    inv = store.begin_invocation("worker")
    assert inv.invocation_id > 0
    store.complete_invocation(inv)
    assert store.load_latest("worker") is None
    assert store.query_versions("worker") == []


def test_in_memory_node_state_store_is_concrete() -> None:
    store = InMemoryNodeStateStore(202)
    assert len(InMemoryNodeStateStore.__abstractmethods__) == 0
    inv = store.begin_invocation("worker")
    store.complete_invocation(inv)
    assert store.load_latest("worker") is not None
    assert store.load_latest_completed("worker") is not None
    assert len(store.query_versions("worker")) == 1


def test_deliver_store_factory_create_returns_required_deliver_store_type() -> None:
    assert get_type_hints(DeliverStoreFactory.create)["return"] is DeliverStore


# ── DeliverStore ABC + DeliverRecord fields ─────


def test_deliver_store_abc_has_active_consumption_api() -> None:
    expected = {
        "accumulate",
        "query_consumable",
        "mark_consumed",
        "promote_staged_by_source",
        "promote_consumed",
    }
    assert set(DeliverStore.__abstractmethods__) == expected


def test_deliver_record_has_active_fields_with_correct_types() -> None:
    hints = get_type_hints(DeliverRecord)
    assert "next_node" not in hints
    assert "source_node_id" in hints
    assert hints["source_node_id"] is str
    assert "source_invocation_id" in hints
    assert hints["source_invocation_id"] is int
    assert "consumed_by_invocation_id" in hints
    assert hints["status"] is DeliverConsumptionStatus


def test_deliver_record_status_defaults_to_pending_and_consumed_by_defaults_to_none() -> None:
    record = DeliverRecord(
        deliver_id=1,
        graph_instance_id=202,
        node_id="worker",
        source_node_id="producer",
        source_invocation_id=99,
        content="payload",
        created_at=1_000,
        updated_at=1_000,
    )
    assert record.status == DeliverConsumptionStatus.PENDING
    assert record.consumed_by_invocation_id is None
    assert record.source_node_id == "producer"
    assert record.source_invocation_id == 99


def test_deliver_record_status_accepts_all_consumption_enum_values() -> None:
    for status in DeliverConsumptionStatus:
        record = DeliverRecord(
            deliver_id=1,
            graph_instance_id=202,
            node_id="worker",
            source_node_id="producer",
            source_invocation_id=99,
            content="payload",
            status=status,
            created_at=1_000,
            updated_at=1_000,
        )
        assert record.status == status


def test_deliver_record_is_frozen_and_extra_forbid() -> None:
    record = DeliverRecord(
        deliver_id=1,
        graph_instance_id=202,
        node_id="worker",
        source_node_id="producer",
        source_invocation_id=99,
        content="payload",
        created_at=1_000,
        updated_at=1_000,
    )
    assert record.model_config.get("frozen") is True
    assert record.model_config.get("extra") == "forbid"
    with pytest.raises(ValidationError):
        record.source_node_id = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        DeliverRecord.model_validate({**record.model_dump(), "unexpected": True})
