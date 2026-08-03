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
    GraphMetadata,
    GraphMetadataStore,
    GraphStateSnapshot,
    InvocationContext,
    InvocationStatus,
    NodeInvocationRecord,
    NodeState,
    NodeStateFactory,
    RecoveryContext,
    SchedulerInstanceStatus,
    SimpleNodeState,
)


def _node_record() -> NodeInvocationRecord:
    return NodeInvocationRecord(
        invocation_id=101,
        graph_instance_id=202,
        node_name="worker",
        version=0,
        parent_version=None,
        status=InvocationStatus.PENDING,
        state_json={"input": "value"},
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
        instance_seq=4,
        iteration_count=5,
        activated_sources={"worker": ["start"]},
        pending_dispatches={"worker": {"start": [{"input": "value"}]}},
    )


def test_distributed_persistence_enums_have_the_specified_values() -> None:
    assert list(SchedulerInstanceStatus) == [
        SchedulerInstanceStatus.DORMANT,
        SchedulerInstanceStatus.READY,
        SchedulerInstanceStatus.RUNNING,
        SchedulerInstanceStatus.COMPLETED,
    ]
    assert list(InvocationStatus) == [
        InvocationStatus.PENDING,
        InvocationStatus.RUNNING,
        InvocationStatus.COMPLETED,
        InvocationStatus.CANCELED,
        InvocationStatus.CRASHED,
        InvocationStatus.SUPERSEDED,
    ]
    assert list(DeliverConsumptionStatus) == [
        DeliverConsumptionStatus.PENDING,
        DeliverConsumptionStatus.CONSUMED,
        DeliverConsumptionStatus.CONSUMED_PENDING,
        DeliverConsumptionStatus.CONSUMED_COMPLETED,
    ]


def test_node_invocation_record_is_frozen_and_defaults_to_not_suspended() -> None:
    record = _node_record()

    assert record.suspended is False
    with pytest.raises(ValidationError):
        record.node_name = "other"
    with pytest.raises(ValidationError):
        NodeInvocationRecord.model_validate({**record.model_dump(), "extra": True})


def test_graph_persistence_value_objects_retain_recovery_and_history_data() -> None:
    record = _node_record()
    metadata = _metadata()

    context = InvocationContext(
        invocation_id=record.invocation_id,
        node_name=record.node_name,
        version=record.version,
        parent_version=record.parent_version,
    )
    recovery = RecoveryContext(
        metadata=metadata,
        node_states={record.node_name: record, "idle": None},
        rebuilt_main_state={"total": 1},
    )
    snapshot = GraphStateSnapshot(metadata=metadata, nodes={record.node_name: [record]})

    assert context.invocation_id == record.invocation_id
    assert recovery.rebuilt_main_state == {"total": 1}
    assert snapshot.nodes[record.node_name] == [record]
    for model in (
        NodeInvocationRecord,
        GraphMetadata,
        InvocationContext,
        RecoveryContext,
        GraphStateSnapshot,
    ):
        assert model.model_config["frozen"] is True
        assert model.model_config["extra"] == "forbid"


def test_persistence_interfaces_are_abstract_with_the_specified_methods() -> None:
    assert issubclass(NodeState, ABC)
    assert issubclass(GraphMetadataStore, ABC)
    assert issubclass(NodeStateFactory, ABC)
    assert issubclass(DeliverStoreFactory, ABC)
    assert set(GraphMetadataStore.__abstractmethods__) == {"save", "load", "update_status"}
    assert set(NodeStateFactory.__abstractmethods__) == {"create"}
    assert set(DeliverStoreFactory.__abstractmethods__) == {"create"}

    with pytest.raises(TypeError):
        GraphMetadataStore()
    with pytest.raises(TypeError):
        NodeStateFactory()
    with pytest.raises(TypeError):
        DeliverStoreFactory()


def test_deliver_store_factory_create_returns_required_deliver_store_type() -> None:
    assert get_type_hints(DeliverStoreFactory.create)["return"] is DeliverStore


def test_simple_node_state_remains_concrete_with_ticket_fourteen_implementation() -> None:
    state = SimpleNodeState()

    assert SimpleNodeState.__abstractmethods__ == frozenset()
    state.save_invocation(202, "worker", 101, 0, None, InvocationStatus.COMPLETED, {"value": 1})
    assert state.load_invocation(202, "worker", 101) == state.load_latest(202, "worker")
    assert state.load_latest_completed(202, "worker") == state.load_latest(202, "worker")
    assert len(state.query_versions(202, "worker")) == 1


# ── DeliverStore ABC evolution + DeliverRecord new fields ─────


def test_deliver_store_abc_has_eight_abstract_methods_including_new_consumption_api() -> None:
    expected = {
        "accumulate",
        "query_consumable",
        "mark_consumed",
        "promote_consumed",
        "query_pending",
        "query_by_target",
        "mark_submitted",
        "clear",
    }
    assert set(DeliverStore.__abstractmethods__) == expected


def test_deliver_record_has_ticket_thirteen_fields_with_correct_types() -> None:
    hints = get_type_hints(DeliverRecord)
    assert "source_node" in hints
    assert hints["source_node"] is str
    assert "source_invocation_id" in hints
    assert hints["source_invocation_id"] is int
    assert "consumed_by_invocation_id" in hints
    assert hints["status"] is DeliverConsumptionStatus


def test_deliver_record_status_defaults_to_pending_and_consumed_by_defaults_to_none() -> None:
    record = DeliverRecord(
        deliver_id=1,
        graph_instance_id=202,
        node_name="worker",
        next_node="",
        source_node="producer",
        source_invocation_id=99,
        content="payload",
        created_at=1_000,
        updated_at=1_000,
    )
    assert record.status == DeliverConsumptionStatus.PENDING
    assert record.consumed_by_invocation_id is None
    assert record.source_node == "producer"
    assert record.source_invocation_id == 99


def test_deliver_record_status_accepts_all_consumption_enum_values() -> None:
    for status in DeliverConsumptionStatus:
        record = DeliverRecord(
            deliver_id=1,
            graph_instance_id=202,
            node_name="worker",
            next_node="",
            source_node="producer",
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
        node_name="worker",
        next_node="",
        source_node="producer",
        source_invocation_id=99,
        content="payload",
        created_at=1_000,
        updated_at=1_000,
    )
    assert record.model_config["frozen"] is True
    assert record.model_config["extra"] == "forbid"
    with pytest.raises(ValidationError):
        record.source_node = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        DeliverRecord.model_validate({**record.model_dump(), "unexpected": True})
