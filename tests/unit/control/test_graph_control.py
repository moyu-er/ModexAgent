# ruff: noqa: ANN401

"""Tests for GraphControlService — external graph instance control.

- ``_deliver`` routes through ``coordinator.route_deliver`` (no shared
  ``deliver_store``). Delivers land in the per-node ``DeliverStore`` inside
  the coordinator.
- The coordinator is fetched via ``coordinator_lookup`` (provided by
  ``GraphOrchestrator._lookup_coordinator`` from ``_active_instances``).
- If no active instance exists for the gid, ``_deliver`` raises
  ``ValueError``.
"""

from __future__ import annotations

from typing import Any

import pytest

from modex_agent.control.graph_control import (
    GraphControlService,
    InMemoryGraphEngineController,
)
from modex_agent.control.graph_recovery import (
    GraphEngineFactory,
    GraphRecoveryService,
)
from modex_agent.control.types import ControlCommand, ControlCommandType, ControlScope
from modex_graph import (
    DeliverConsumptionStatus,
    DeliverStore,
    GraphInstance,
    GraphInstanceStatus,
    GraphMetadata,
    GraphPersistenceCoordinator,
    InMemoryGraphInstanceStore,
    create_null_coordinator,
)

_SESSION_ID = "sess-1:main"
_SPEC_ID = 1001
_GID = 9001


class _RecordingEngineFactory(GraphEngineFactory):
    """Concrete factory that records create_and_run calls."""

    def __init__(self) -> None:
        self.calls: list[GraphInstance] = []

    async def create_and_run(self, instance: GraphInstance) -> None:
        self.calls.append(instance)


def _make_command(
    cmd_type: ControlCommandType,
    *,
    graph_instance_id: int | None = _GID,
    payload: dict[str, object] | None = None,
    command_id: str = "cmd-1",
) -> ControlCommand:
    return ControlCommand(
        command_id=command_id,
        type=cmd_type,
        scope=ControlScope(
            session_id=_SESSION_ID,
            graph_instance_id=graph_instance_id,
        ),
        payload=payload if payload is not None else {},
    )


def _make_instance(graph_instance_id: int = _GID, status: str = "running") -> GraphInstance:
    return GraphInstance(
        GraphMetadata(
            graph_instance_id=graph_instance_id,
            spec_id=_SPEC_ID,
            parent_instance_id=None,
            parent_node=None,
            status=GraphInstanceStatus(status),
        ),
        create_null_coordinator(graph_instance_id),
    )


def _load_status(store: InMemoryGraphInstanceStore, gid: int = _GID) -> str:
    instance = store.load(gid)
    assert instance is not None
    return instance.status


def _make_coordinator_lookup(
    instance: GraphInstance | None,
) -> Any:
    """Build a coordinator_lookup callable matching the instance's gid."""
    if instance is None:
        return lambda gid: None
    gid = instance.graph_instance_id
    coordinator = instance.coordinator
    return lambda lookup_gid: coordinator if lookup_gid == gid else None


def _get_deliver_store(coordinator: GraphPersistenceCoordinator, node_name: str) -> DeliverStore:
    store = coordinator.get_deliver_store(node_name)
    assert store is not None, f"Node {node_name!r} not registered on coordinator"
    return store


def _make_service(
    *,
    instance: GraphInstance | None = None,
    engine: InMemoryGraphEngineController | None = None,
    register_nodes: list[str] | None = None,
) -> tuple[GraphControlService, InMemoryGraphInstanceStore, GraphInstance | None]:
    instance_store = InMemoryGraphInstanceStore()
    if instance is not None:
        instance_store.save(instance.metadata)
        if register_nodes:
            for node_name in register_nodes:
                instance.coordinator.register_node(node_name)
    coordinator_lookup = _make_coordinator_lookup(instance)
    recovery_service = GraphRecoveryService(instance_store, _RecordingEngineFactory())
    service = GraphControlService(instance_store, recovery_service, coordinator_lookup)
    if engine is not None:
        service.register_engine(engine)
    return service, instance_store, instance


# -- handle() routing --------------------------------------------------------


class TestHandleRouting:
    @pytest.mark.asyncio
    async def test_pause_routes_to_pause(self) -> None:
        engine = InMemoryGraphEngineController(_GID)
        service, instance_store, _ = _make_service(instance=_make_instance(), engine=engine)
        await service.handle(_make_command(ControlCommandType.PAUSE_GRAPH))
        assert engine.pause_called is True
        assert engine.stop_called is False
        assert engine.resume_called is False
        assert _load_status(instance_store) == GraphInstanceStatus.PAUSED

    @pytest.mark.asyncio
    async def test_stop_routes_to_stop(self) -> None:
        engine = InMemoryGraphEngineController(_GID)
        service, instance_store, _ = _make_service(instance=_make_instance(), engine=engine)
        await service.handle(_make_command(ControlCommandType.STOP_GRAPH))
        assert engine.stop_called is True
        assert engine.pause_called is False
        assert _load_status(instance_store) == GraphInstanceStatus.STOPPED

    @pytest.mark.asyncio
    async def test_resume_routes_to_resume(self) -> None:
        engine = InMemoryGraphEngineController(_GID)
        service, instance_store, _ = _make_service(
            instance=_make_instance(status="paused"), engine=engine
        )
        await service.handle(_make_command(ControlCommandType.RESUME_GRAPH))
        assert _load_status(instance_store) == GraphInstanceStatus.RUNNING

    @pytest.mark.asyncio
    async def test_deliver_routes_to_deliver(self) -> None:
        engine = InMemoryGraphEngineController(_GID)
        instance = _make_instance()
        service, _, _ = _make_service(
            instance=instance, engine=engine, register_nodes=["worker"]
        )
        await service.handle(
            _make_command(
                ControlCommandType.DELIVER_TO_NODE,
                payload={"node_name": "worker", "content": {"k": "v"}},
            )
        )
        assert len(engine.deliver_calls) == 1
        assert engine.deliver_calls[0] == ("worker", {"k": "v"})
        store = _get_deliver_store(instance.coordinator, "worker")
        pending = store.query_consumable(_GID, "worker")
        assert len(pending) == 1
        assert pending[0].content == {"k": "v"}
        assert pending[0].node_name == "worker"
        assert pending[0].source_node == "__external__"
        assert pending[0].source_invocation_id == 0
        assert pending[0].status == DeliverConsumptionStatus.PENDING

    @pytest.mark.asyncio
    async def test_non_graph_command_is_ignored(self) -> None:
        engine = InMemoryGraphEngineController(_GID)
        service, instance_store, instance = _make_service(
            instance=_make_instance(), engine=engine, register_nodes=["worker"]
        )
        await service.handle(_make_command(ControlCommandType.CANCEL_TURN))
        assert engine.pause_called is False
        assert engine.stop_called is False
        assert engine.resume_called is False
        assert len(engine.deliver_calls) == 0
        assert _load_status(instance_store) == "running"
        if instance is not None:
            store = _get_deliver_store(instance.coordinator, "worker")
            assert store.query_consumable(_GID, "worker") == []


# -- PAUSE --------------------------------------------------------------------


class TestPause:
    @pytest.mark.asyncio
    async def test_pauses_running_instance(self) -> None:
        service, instance_store, _ = _make_service(instance=_make_instance(status="running"))
        await service.handle(_make_command(ControlCommandType.PAUSE_GRAPH))
        assert _load_status(instance_store) == GraphInstanceStatus.PAUSED

    @pytest.mark.asyncio
    async def test_calls_engine_pause_when_registered(self) -> None:
        engine = InMemoryGraphEngineController(_GID)
        service, _, _ = _make_service(instance=_make_instance(), engine=engine)
        await service.handle(_make_command(ControlCommandType.PAUSE_GRAPH))
        assert engine.pause_called is True

    @pytest.mark.asyncio
    async def test_succeeds_without_engine_registered(self) -> None:
        service, instance_store, _ = _make_service(instance=_make_instance())
        await service.handle(_make_command(ControlCommandType.PAUSE_GRAPH))
        assert _load_status(instance_store) == GraphInstanceStatus.PAUSED

    @pytest.mark.asyncio
    async def test_raises_when_graph_instance_id_missing(self) -> None:
        service, _, _ = _make_service(instance=_make_instance())
        cmd = ControlCommand(
            command_id="no-gid",
            type=ControlCommandType.PAUSE_GRAPH,
            scope=ControlScope(session_id=_SESSION_ID),
        )
        with pytest.raises(ValueError, match="scope.graph_instance_id"):
            await service.handle(cmd)


# -- STOP ---------------------------------------------------------------------


class TestStop:
    @pytest.mark.asyncio
    async def test_stops_running_instance(self) -> None:
        service, instance_store, _ = _make_service(instance=_make_instance(status="running"))
        await service.handle(_make_command(ControlCommandType.STOP_GRAPH))
        assert _load_status(instance_store) == GraphInstanceStatus.STOPPED

    @pytest.mark.asyncio
    async def test_calls_engine_stop_when_registered(self) -> None:
        engine = InMemoryGraphEngineController(_GID)
        service, _, _ = _make_service(instance=_make_instance(), engine=engine)
        await service.handle(_make_command(ControlCommandType.STOP_GRAPH))
        assert engine.stop_called is True

    @pytest.mark.asyncio
    async def test_does_not_call_pause(self) -> None:
        engine = InMemoryGraphEngineController(_GID)
        service, _, _ = _make_service(instance=_make_instance(), engine=engine)
        await service.handle(_make_command(ControlCommandType.STOP_GRAPH))
        assert engine.pause_called is False


# -- RESUME -------------------------------------------------------------------


class TestResume:
    @pytest.mark.asyncio
    async def test_resumes_paused_instance_to_running(self) -> None:
        service, instance_store, _ = _make_service(instance=_make_instance(status="paused"))
        await service.handle(_make_command(ControlCommandType.RESUME_GRAPH))
        assert _load_status(instance_store) == GraphInstanceStatus.RUNNING

    @pytest.mark.asyncio
    async def test_resumes_stopped_instance_to_running(self) -> None:
        service, instance_store, _ = _make_service(instance=_make_instance(status="stopped"))
        await service.handle(_make_command(ControlCommandType.RESUME_GRAPH))
        assert _load_status(instance_store) == GraphInstanceStatus.RUNNING


# -- DELIVER_TO_NODE -------------------------------------


class TestDeliver:
    @pytest.mark.asyncio
    async def test_routes_through_coordinator_route_deliver(self) -> None:
        instance = _make_instance()
        service, _, _ = _make_service(
            instance=instance, register_nodes=["summarizer"]
        )
        await service.handle(
            _make_command(
                ControlCommandType.DELIVER_TO_NODE,
                payload={"node_name": "summarizer", "content": "hello"},
            )
        )
        store = _get_deliver_store(instance.coordinator, "summarizer")
        pending = store.query_consumable(_GID, "summarizer")
        assert len(pending) == 1
        record = pending[0]
        assert record.node_name == "summarizer"
        assert record.content == "hello"
        assert record.source_node == "__external__"
        assert record.source_invocation_id == 0
        assert record.status == DeliverConsumptionStatus.PENDING

    @pytest.mark.asyncio
    async def test_notifies_engine_when_registered(self) -> None:
        engine = InMemoryGraphEngineController(_GID)
        service, _, _ = _make_service(
            instance=_make_instance(), engine=engine, register_nodes=["worker"]
        )
        await service.handle(
            _make_command(
                ControlCommandType.DELIVER_TO_NODE,
                payload={"node_name": "worker", "content": 42},
            )
        )
        assert engine.deliver_calls == [("worker", 42)]

    @pytest.mark.asyncio
    async def test_routes_even_without_engine(self) -> None:
        instance = _make_instance()
        service, _, _ = _make_service(
            instance=instance, register_nodes=["worker"]
        )
        await service.handle(
            _make_command(
                ControlCommandType.DELIVER_TO_NODE,
                payload={"node_name": "worker", "content": [1, 2, 3]},
            )
        )
        store = _get_deliver_store(instance.coordinator, "worker")
        pending = store.query_consumable(_GID, "worker")
        assert len(pending) == 1
        assert pending[0].content == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_multiple_delivers_accumulate_in_order(self) -> None:
        instance = _make_instance()
        service, _, _ = _make_service(
            instance=instance, register_nodes=["worker"]
        )
        for i in range(3):
            await service.handle(
                _make_command(
                    ControlCommandType.DELIVER_TO_NODE,
                    payload={"node_name": "worker", "content": i},
                    command_id=f"cmd-{i}",
                )
            )
        store = _get_deliver_store(instance.coordinator, "worker")
        pending = store.query_consumable(_GID, "worker")
        assert [r.content for r in pending] == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_deliver_to_distinct_nodes_isolated(self) -> None:
        instance = _make_instance()
        service, _, _ = _make_service(
            instance=instance, register_nodes=["alpha", "beta"]
        )
        await service.handle(
            _make_command(
                ControlCommandType.DELIVER_TO_NODE,
                payload={"node_name": "alpha", "content": "a"},
            )
        )
        await service.handle(
            _make_command(
                ControlCommandType.DELIVER_TO_NODE,
                payload={"node_name": "beta", "content": "b"},
                command_id="cmd-2",
            )
        )
        alpha_store = _get_deliver_store(instance.coordinator, "alpha")
        beta_store = _get_deliver_store(instance.coordinator, "beta")
        assert len(alpha_store.query_consumable(_GID, "alpha")) == 1
        assert len(beta_store.query_consumable(_GID, "beta")) == 1
        assert alpha_store.query_consumable(_GID, "alpha")[0].content == "a"
        assert beta_store.query_consumable(_GID, "beta")[0].content == "b"

    @pytest.mark.asyncio
    async def test_raises_when_node_name_missing(self) -> None:
        service, _, _ = _make_service(instance=_make_instance())
        with pytest.raises(ValueError, match="payload\\['node_name'\\]"):
            await service.handle(
                _make_command(
                    ControlCommandType.DELIVER_TO_NODE,
                    payload={},
                )
            )

    @pytest.mark.asyncio
    async def test_raises_when_node_name_not_str(self) -> None:
        service, _, _ = _make_service(instance=_make_instance())
        with pytest.raises(ValueError, match="payload\\['node_name'\\]"):
            await service.handle(
                _make_command(
                    ControlCommandType.DELIVER_TO_NODE,
                    payload={"node_name": 123, "content": "x"},
                )
            )

    @pytest.mark.asyncio
    async def test_raises_when_graph_instance_id_missing(self) -> None:
        service, _, _ = _make_service(instance=_make_instance())
        cmd = ControlCommand(
            command_id="no-gid",
            type=ControlCommandType.DELIVER_TO_NODE,
            scope=ControlScope(session_id=_SESSION_ID),
            payload={"node_name": "worker", "content": "x"},
        )
        with pytest.raises(ValueError, match="scope.graph_instance_id"):
            await service.handle(cmd)

    @pytest.mark.asyncio
    async def test_raises_when_no_active_instance(self) -> None:
        """Deliver to unknown gid raises ValueError (no coordinator)."""
        service, _, _ = _make_service()
        with pytest.raises(ValueError, match="No active graph instance"):
            await service.handle(
                _make_command(
                    ControlCommandType.DELIVER_TO_NODE,
                    payload={"node_name": "worker", "content": "x"},
                )
            )

    @pytest.mark.asyncio
    async def test_content_none_is_valid_deliver(self) -> None:
        instance = _make_instance()
        service, _, _ = _make_service(
            instance=instance, register_nodes=["worker"]
        )
        await service.handle(
            _make_command(
                ControlCommandType.DELIVER_TO_NODE,
                payload={"node_name": "worker", "content": None},
            )
        )
        store = _get_deliver_store(instance.coordinator, "worker")
        pending = store.query_consumable(_GID, "worker")
        assert len(pending) == 1
        assert pending[0].content is None

    @pytest.mark.asyncio
    async def test_does_not_update_instance_status(self) -> None:
        service, instance_store, _ = _make_service(
            instance=_make_instance(status="running"), register_nodes=["worker"]
        )
        await service.handle(
            _make_command(
                ControlCommandType.DELIVER_TO_NODE,
                payload={"node_name": "worker", "content": "x"},
            )
        )
        assert _load_status(instance_store) == "running"


# -- Engine registration -----------------------------------------------------


class TestEngineRegistration:
    def test_register_engine_stores_by_graph_instance_id(self) -> None:
        service, _, _ = _make_service()
        engine = InMemoryGraphEngineController(_GID)
        service.register_engine(engine)
        assert _GID in service._engines

    def test_unregister_engine_removes_controller(self) -> None:
        service, _, _ = _make_service()
        engine = InMemoryGraphEngineController(_GID)
        service.register_engine(engine)
        service.unregister_engine(_GID)
        assert _GID not in service._engines

    def test_unregister_unknown_id_is_noop(self) -> None:
        service, _, _ = _make_service()
        service.unregister_engine(9999)

    @pytest.mark.asyncio
    async def test_only_matching_engine_is_called(self) -> None:
        engine_a = InMemoryGraphEngineController(_GID)
        engine_b = InMemoryGraphEngineController(8888)
        service, instance_store, _ = _make_service()
        instance_store.save(_make_instance(graph_instance_id=_GID).metadata)
        instance_store.save(_make_instance(graph_instance_id=8888).metadata)
        service.register_engine(engine_a)
        service.register_engine(engine_b)
        await service.handle(_make_command(ControlCommandType.PAUSE_GRAPH))
        assert engine_a.pause_called is True
        assert engine_b.pause_called is False
