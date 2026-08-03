"""Tests for GraphControlService — external graph instance control (ticket 10 §3.3)."""

from __future__ import annotations

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
    DeliverStatus,
    GraphInstance,
    GraphInstanceStatus,
    InMemoryDeliverStore,
    InMemoryGraphInstanceStore,
    MemoryCheckpointStore,
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
        graph_instance_id=graph_instance_id,
        spec_id=_SPEC_ID,
        status=status,
    )


def _load_status(store: InMemoryGraphInstanceStore, gid: int = _GID) -> str:
    instance = store.load_by_id(gid)
    assert instance is not None
    return instance.status


def _make_service(
    *,
    instance: GraphInstance | None = None,
    engine: InMemoryGraphEngineController | None = None,
) -> tuple[GraphControlService, InMemoryGraphInstanceStore, InMemoryDeliverStore]:
    instance_store = InMemoryGraphInstanceStore()
    deliver_store = InMemoryDeliverStore()
    if instance is not None:
        instance_store.save(instance)
    recovery_service = GraphRecoveryService(
        instance_store, MemoryCheckpointStore(), _RecordingEngineFactory()
    )
    service = GraphControlService(instance_store, deliver_store, recovery_service)
    if engine is not None:
        service.register_engine(engine)
    return service, instance_store, deliver_store


# ── handle() routing ────────────────────────────────────────────────────


class TestHandleRouting:
    @pytest.mark.asyncio
    async def test_pause_routes_to_pause(self) -> None:
        engine = InMemoryGraphEngineController(_GID)
        service, instance_store, _ = _make_service(
            instance=_make_instance(), engine=engine
        )
        await service.handle(_make_command(ControlCommandType.PAUSE_GRAPH))
        assert engine.pause_called is True
        assert engine.stop_called is False
        assert engine.resume_called is False
        assert _load_status(instance_store) == GraphInstanceStatus.PAUSED

    @pytest.mark.asyncio
    async def test_stop_routes_to_stop(self) -> None:
        engine = InMemoryGraphEngineController(_GID)
        service, instance_store, _ = _make_service(
            instance=_make_instance(), engine=engine
        )
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
        service, _, deliver_store = _make_service(
            instance=_make_instance(), engine=engine
        )
        await service.handle(
            _make_command(
                ControlCommandType.DELIVER_TO_NODE,
                payload={"node_name": "worker", "content": {"k": "v"}},
            )
        )
        assert len(engine.deliver_calls) == 1
        assert engine.deliver_calls[0] == ("worker", {"k": "v"})
        pending = deliver_store.query_pending(_GID, "worker")
        assert len(pending) == 1
        assert pending[0].content == {"k": "v"}
        assert pending[0].next_node == ""
        assert pending[0].status == DeliverStatus.ACCUMULATED

    @pytest.mark.asyncio
    async def test_non_graph_command_is_ignored(self) -> None:
        engine = InMemoryGraphEngineController(_GID)
        service, instance_store, deliver_store = _make_service(
            instance=_make_instance(), engine=engine
        )
        await service.handle(_make_command(ControlCommandType.CANCEL_TURN))
        assert engine.pause_called is False
        assert engine.stop_called is False
        assert engine.resume_called is False
        assert len(engine.deliver_calls) == 0
        assert _load_status(instance_store) == "running"
        assert deliver_store.query_pending(_GID, "worker") == []


# ── PAUSE ───────────────────────────────────────────────────────────────


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


# ── STOP ────────────────────────────────────────────────────────────────


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


# ── RESUME ──────────────────────────────────────────────────────────────


class TestResume:
    @pytest.mark.asyncio
    async def test_resumes_paused_instance_to_running(self) -> None:
        service, instance_store, _ = _make_service(
            instance=_make_instance(status="paused")
        )
        await service.handle(_make_command(ControlCommandType.RESUME_GRAPH))
        assert _load_status(instance_store) == GraphInstanceStatus.RUNNING

    @pytest.mark.asyncio
    async def test_resumes_stopped_instance_to_running(self) -> None:
        service, instance_store, _ = _make_service(
            instance=_make_instance(status="stopped")
        )
        await service.handle(_make_command(ControlCommandType.RESUME_GRAPH))
        assert _load_status(instance_store) == GraphInstanceStatus.RUNNING


# ── DELIVER_TO_NODE ─────────────────────────────────────────────────────


class TestDeliver:
    @pytest.mark.asyncio
    async def test_accumulates_into_deliver_store(self) -> None:
        service, _, deliver_store = _make_service(instance=_make_instance())
        await service.handle(
            _make_command(
                ControlCommandType.DELIVER_TO_NODE,
                payload={"node_name": "summarizer", "content": "hello"},
            )
        )
        pending = deliver_store.query_pending(_GID, "summarizer")
        assert len(pending) == 1
        record = pending[0]
        assert record.node_name == "summarizer"
        assert record.content == "hello"
        assert record.next_node == ""
        assert record.status == DeliverStatus.ACCUMULATED

    @pytest.mark.asyncio
    async def test_notifies_engine_when_registered(self) -> None:
        engine = InMemoryGraphEngineController(_GID)
        service, _, _ = _make_service(instance=_make_instance(), engine=engine)
        await service.handle(
            _make_command(
                ControlCommandType.DELIVER_TO_NODE,
                payload={"node_name": "worker", "content": 42},
            )
        )
        assert engine.deliver_calls == [("worker", 42)]

    @pytest.mark.asyncio
    async def test_accumulates_even_without_engine(self) -> None:
        service, _, deliver_store = _make_service(instance=_make_instance())
        await service.handle(
            _make_command(
                ControlCommandType.DELIVER_TO_NODE,
                payload={"node_name": "worker", "content": [1, 2, 3]},
            )
        )
        pending = deliver_store.query_pending(_GID, "worker")
        assert len(pending) == 1
        assert pending[0].content == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_multiple_delivers_accumulate_in_order(self) -> None:
        service, _, deliver_store = _make_service(instance=_make_instance())
        for i in range(3):
            await service.handle(
                _make_command(
                    ControlCommandType.DELIVER_TO_NODE,
                    payload={"node_name": "worker", "content": i},
                    command_id=f"cmd-{i}",
                )
            )
        pending = deliver_store.query_pending(_GID, "worker")
        assert [r.content for r in pending] == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_deliver_to_distinct_nodes_isolated(self) -> None:
        service, _, deliver_store = _make_service(instance=_make_instance())
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
        assert len(deliver_store.query_pending(_GID, "alpha")) == 1
        assert len(deliver_store.query_pending(_GID, "beta")) == 1
        assert deliver_store.query_pending(_GID, "alpha")[0].content == "a"
        assert deliver_store.query_pending(_GID, "beta")[0].content == "b"

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
    async def test_content_none_is_valid_deliver(self) -> None:
        service, _, deliver_store = _make_service(instance=_make_instance())
        await service.handle(
            _make_command(
                ControlCommandType.DELIVER_TO_NODE,
                payload={"node_name": "worker", "content": None},
            )
        )
        pending = deliver_store.query_pending(_GID, "worker")
        assert len(pending) == 1
        assert pending[0].content is None

    @pytest.mark.asyncio
    async def test_does_not_update_instance_status(self) -> None:
        service, instance_store, _ = _make_service(instance=_make_instance(status="running"))
        await service.handle(
            _make_command(
                ControlCommandType.DELIVER_TO_NODE,
                payload={"node_name": "worker", "content": "x"},
            )
        )
        assert _load_status(instance_store) == "running"


# ── Engine registration ─────────────────────────────────────────────────


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
        instance_store.save(_make_instance(graph_instance_id=_GID))
        instance_store.save(_make_instance(graph_instance_id=8888))
        service.register_engine(engine_a)
        service.register_engine(engine_b)
        await service.handle(_make_command(ControlCommandType.PAUSE_GRAPH))
        assert engine_a.pause_called is True
        assert engine_b.pause_called is False
