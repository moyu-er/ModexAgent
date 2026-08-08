"""Tests for GraphRecoveryService — auto + manual recovery."""

from __future__ import annotations

import pytest

from modex_agent.control.graph_control import (
    GraphControlService,
    InMemoryGraphEngineController,
)
from modex_agent.control.graph_recovery import GraphRecoveryService
from modex_agent.control.types import ControlCommand, ControlCommandType, ControlScope
from modex_graph import (
    CoordinatorFactory,
    GraphInstance,
    GraphInstanceStatus,
    GraphInstanceStore,
    GraphMetadata,
    GraphPersistenceCoordinator,
    InMemoryGraphInstanceStore,
    NullCoordinatorFactory,
    create_null_coordinator,
)

_SPEC_ID = 1001
_SESSION_ID = "sess-1:main"


def _make_instance(
    graph_instance_id: int,
    *,
    status: str = GraphInstanceStatus.RUNNING.value,
) -> GraphInstance:
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


def _make_resume_command(gid: int, *, command_id: str = "cmd-1") -> ControlCommand:
    return ControlCommand(
        command_id=command_id,
        type=ControlCommandType.RESUME_GRAPH,
        scope=ControlScope(session_id=_SESSION_ID, graph_instance_id=gid),
    )


class _RecordingOrchestrator:
    """Mock orchestrator that records _run_existing_instance calls."""

    def __init__(self) -> None:
        self.calls: list[GraphInstance] = []

    async def _run_existing_instance(self, instance: GraphInstance) -> None:
        self.calls.append(instance)


class _FailingOrchestrator:
    async def _run_existing_instance(self, instance: GraphInstance) -> None:
        raise RuntimeError(f"recovery setup failed for {instance.graph_instance_id}")


def _make_recovery_service(
    *,
    instances: list[GraphInstance] | None = None,
    orchestrator: _RecordingOrchestrator | None = None,
) -> tuple[
    GraphRecoveryService,
    InMemoryGraphInstanceStore,
    _RecordingOrchestrator,
]:
    instance_store = InMemoryGraphInstanceStore()
    for inst in instances or []:
        instance_store.save(inst.metadata)
    orchestrator = orchestrator or _RecordingOrchestrator()
    service = GraphRecoveryService(
        instance_store, orchestrator,  # type: ignore[arg-type]
        coordinator_factory=NullCoordinatorFactory(),
    )
    return service, instance_store, orchestrator


# -- recover_crashed (auto) -------------------------------------------------


class TestRecoverCrashed:
    @pytest.mark.asyncio
    async def test_picks_crashed_and_orphan_running(self) -> None:
        crashed_a = _make_instance(1001, status=GraphInstanceStatus.CRASHED.value)
        crashed_b = _make_instance(1002, status=GraphInstanceStatus.CRASHED.value)
        running = _make_instance(1003, status=GraphInstanceStatus.RUNNING.value)
        paused = _make_instance(1004, status=GraphInstanceStatus.PAUSED.value)
        completed = _make_instance(1005, status=GraphInstanceStatus.COMPLETED.value)
        service, _, orchestrator = _make_recovery_service(
            instances=[crashed_a, crashed_b, running, paused, completed]
        )

        recovered = await service.recover_crashed()

        assert sorted(recovered) == [1001, 1002, 1003]
        assert len(orchestrator.calls) == 3
        assert {c.graph_instance_id for c in orchestrator.calls} == {1001, 1002, 1003}

    @pytest.mark.asyncio
    async def test_sets_status_to_running(self) -> None:
        crashed = _make_instance(9001, status=GraphInstanceStatus.CRASHED.value)
        service, instance_store, _ = _make_recovery_service(instances=[crashed])

        await service.recover_crashed()

        instance = instance_store.load(9001)
        assert instance is not None
        assert instance.status == GraphInstanceStatus.RUNNING.value

    @pytest.mark.asyncio
    async def test_returns_recovered_ids(self) -> None:
        crashed_a = _make_instance(7001, status=GraphInstanceStatus.CRASHED.value)
        crashed_b = _make_instance(7002, status=GraphInstanceStatus.CRASHED.value)
        service, _, _ = _make_recovery_service(instances=[crashed_a, crashed_b])

        recovered = await service.recover_crashed()

        assert sorted(recovered) == [7001, 7002]

    @pytest.mark.asyncio
    async def test_no_crashed_or_running_returns_empty_list(self) -> None:
        paused = _make_instance(8002, status=GraphInstanceStatus.PAUSED.value)
        completed = _make_instance(8003, status=GraphInstanceStatus.COMPLETED.value)
        service, _, orchestrator = _make_recovery_service(instances=[paused, completed])

        recovered = await service.recover_crashed()

        assert recovered == []
        assert orchestrator.calls == []

    @pytest.mark.asyncio
    async def test_does_not_pick_paused_or_stopped(self) -> None:
        """Auto recovery must NOT pick PAUSED/STOPPED -- manual resume only."""
        paused = _make_instance(5001, status=GraphInstanceStatus.PAUSED.value)
        stopped = _make_instance(5002, status=GraphInstanceStatus.STOPPED.value)
        service, _, orchestrator = _make_recovery_service(instances=[paused, stopped])

        recovered = await service.recover_crashed()

        assert recovered == []
        assert orchestrator.calls == []

    @pytest.mark.asyncio
    async def test_calls_orchestrator_in_insertion_order(self) -> None:
        crashed_a = _make_instance(4001, status=GraphInstanceStatus.CRASHED.value)
        crashed_b = _make_instance(4002, status=GraphInstanceStatus.CRASHED.value)
        service, _, orchestrator = _make_recovery_service(instances=[crashed_a, crashed_b])

        await service.recover_crashed()

        assert [c.graph_instance_id for c in orchestrator.calls] == [4001, 4002]

    @pytest.mark.asyncio
    async def test_marks_instance_crashed_when_recovery_fails(self) -> None:
        crashed = _make_instance(4003, status=GraphInstanceStatus.CRASHED.value)
        instance_store = InMemoryGraphInstanceStore()
        instance_store.save(crashed.metadata)
        service = GraphRecoveryService(
            instance_store, _FailingOrchestrator(),  # type: ignore[arg-type]
            coordinator_factory=NullCoordinatorFactory(),
        )

        recovered = await service.recover_crashed()

        metadata = instance_store.load(4003)
        assert metadata is not None
        assert metadata.status == GraphInstanceStatus.CRASHED
        assert recovered == []


# -- resume (manual) --------------------------------------------------------


class TestResume:
    @pytest.mark.asyncio
    async def test_resumes_paused_instance(self) -> None:
        paused = _make_instance(3001, status=GraphInstanceStatus.PAUSED.value)
        service, instance_store, orchestrator = _make_recovery_service(instances=[paused])

        await service.resume(3001)

        instance = instance_store.load(3001)
        assert instance is not None
        assert instance.status == GraphInstanceStatus.RUNNING.value
        assert len(orchestrator.calls) == 1
        assert orchestrator.calls[0].graph_instance_id == 3001

    @pytest.mark.asyncio
    async def test_rejects_stopped_instance(self) -> None:
        """STOPPED is a terminal status (manual termination) -- not resumable."""
        stopped = _make_instance(3002, status=GraphInstanceStatus.STOPPED.value)
        service, instance_store, orchestrator = _make_recovery_service(instances=[stopped])

        with pytest.raises(ValueError, match="STOPPED is a terminal status"):
            await service.resume(3002)

        instance = instance_store.load(3002)
        assert instance is not None
        assert instance.status == GraphInstanceStatus.STOPPED.value
        assert orchestrator.calls == []

    @pytest.mark.asyncio
    async def test_raises_when_instance_not_found(self) -> None:
        service, _, orchestrator = _make_recovery_service()

        with pytest.raises(ValueError, match="not found"):
            await service.resume(9999)
        assert orchestrator.calls == []

    @pytest.mark.asyncio
    async def test_raises_when_status_is_running(self) -> None:
        running = _make_instance(3003, status=GraphInstanceStatus.RUNNING.value)
        service, _, orchestrator = _make_recovery_service(instances=[running])

        with pytest.raises(ValueError, match="only PAUSED"):
            await service.resume(3003)
        assert orchestrator.calls == []

    @pytest.mark.asyncio
    async def test_raises_when_status_is_crashed(self) -> None:
        """CRASHED instances are auto-recovered only -- not manual resume."""
        crashed = _make_instance(3004, status=GraphInstanceStatus.CRASHED.value)
        service, _, orchestrator = _make_recovery_service(instances=[crashed])

        with pytest.raises(ValueError, match="only PAUSED"):
            await service.resume(3004)
        assert orchestrator.calls == []

    @pytest.mark.asyncio
    async def test_raises_when_status_is_completed(self) -> None:
        completed = _make_instance(3005, status=GraphInstanceStatus.COMPLETED.value)
        service, _, orchestrator = _make_recovery_service(instances=[completed])

        with pytest.raises(ValueError, match="only PAUSED"):
            await service.resume(3005)
        assert orchestrator.calls == []

    @pytest.mark.asyncio
    async def test_raises_when_status_is_failed(self) -> None:
        failed = _make_instance(3006, status=GraphInstanceStatus.FAILED.value)
        service, _, orchestrator = _make_recovery_service(instances=[failed])

        with pytest.raises(ValueError, match="only PAUSED"):
            await service.resume(3006)
        assert orchestrator.calls == []

    @pytest.mark.asyncio
    async def test_does_not_update_status_on_validation_failure(self) -> None:
        running = _make_instance(3007, status=GraphInstanceStatus.RUNNING.value)
        service, instance_store, _ = _make_recovery_service(instances=[running])

        with pytest.raises(ValueError):
            await service.resume(3007)

        instance = instance_store.load(3007)
        assert instance is not None
        assert instance.status == GraphInstanceStatus.RUNNING.value


# -- resume status matrix ---------------------------------------------------


class TestResumeStatusMatrix:
    """Authoritative status matrix: only PAUSED can be manually resumed.

    Iterates all 6 ``GraphInstanceStatus`` values. PAUSED -> resume
    succeeds (status -> RUNNING, orchestrator called). All others ->
    ``ValueError`` raised, status unchanged, orchestrator NOT called.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [
            GraphInstanceStatus.STOPPED,
            GraphInstanceStatus.CRASHED,
            GraphInstanceStatus.COMPLETED,
            GraphInstanceStatus.FAILED,
            GraphInstanceStatus.RUNNING,
        ],
    )
    async def test_non_paused_status_rejected(self, status: GraphInstanceStatus) -> None:
        gid = 9100
        instance = _make_instance(gid, status=status.value)
        service, instance_store, orchestrator = _make_recovery_service(instances=[instance])

        with pytest.raises(ValueError, match="only PAUSED"):
            await service.resume(gid)

        loaded = instance_store.load(gid)
        assert loaded is not None
        assert loaded.status == status.value
        assert orchestrator.calls == []

    @pytest.mark.asyncio
    async def test_paused_status_succeeds(self) -> None:
        gid = 9101
        paused = _make_instance(gid, status=GraphInstanceStatus.PAUSED.value)
        service, instance_store, orchestrator = _make_recovery_service(instances=[paused])

        await service.resume(gid)

        loaded = instance_store.load(gid)
        assert loaded is not None
        assert loaded.status == GraphInstanceStatus.RUNNING.value
        assert len(orchestrator.calls) == 1
        assert orchestrator.calls[0].graph_instance_id == gid

    @pytest.mark.asyncio
    async def test_stopped_rejected_with_terminal_message(self) -> None:
        """STOPPED gets a specific terminal-status message, not the generic one."""
        gid = 9102
        stopped = _make_instance(gid, status=GraphInstanceStatus.STOPPED.value)
        service, _, orchestrator = _make_recovery_service(instances=[stopped])

        with pytest.raises(ValueError, match="STOPPED is a terminal status"):
            await service.resume(gid)
        assert orchestrator.calls == []


# -- GraphControlService delegation -----------------------------------------


class TestControlServiceDelegation:
    @pytest.mark.asyncio
    async def test_resume_delegates_to_recovery_service_when_wired(self) -> None:
        paused = _make_instance(6001, status=GraphInstanceStatus.PAUSED.value)
        instance_store = InMemoryGraphInstanceStore()
        instance_store.save(paused.metadata)
        orchestrator = _RecordingOrchestrator()
        recovery = GraphRecoveryService(
            instance_store, orchestrator,  # type: ignore[arg-type]
            coordinator_factory=NullCoordinatorFactory(),
        )
        service = GraphControlService(
            instance_store, recovery, coordinator_lookup=lambda gid: None
        )

        await service.handle(_make_resume_command(6001))

        instance = instance_store.load(6001)
        assert instance is not None
        assert instance.status == GraphInstanceStatus.RUNNING.value
        assert len(orchestrator.calls) == 1
        assert orchestrator.calls[0].graph_instance_id == 6001

    @pytest.mark.asyncio
    async def test_resume_propagates_validation_error_from_recovery(self) -> None:
        """If recovery_service raises, the error propagates through handle()."""
        running = _make_instance(6003, status=GraphInstanceStatus.RUNNING.value)
        instance_store = InMemoryGraphInstanceStore()
        instance_store.save(running.metadata)
        orchestrator = _RecordingOrchestrator()
        recovery = GraphRecoveryService(
            instance_store, orchestrator,  # type: ignore[arg-type]
            coordinator_factory=NullCoordinatorFactory(),
        )
        service = GraphControlService(
            instance_store, recovery, coordinator_lookup=lambda gid: None
        )

        with pytest.raises(ValueError, match="only PAUSED"):
            await service.handle(_make_resume_command(6003))
        assert orchestrator.calls == []

    @pytest.mark.asyncio
    async def test_resume_does_not_call_engine_controller_when_wired(self) -> None:
        """When recovery_service is wired, the in-memory controller is bypassed."""
        paused = _make_instance(6004, status=GraphInstanceStatus.PAUSED.value)
        instance_store = InMemoryGraphInstanceStore()
        instance_store.save(paused.metadata)
        orchestrator = _RecordingOrchestrator()
        recovery = GraphRecoveryService(
            instance_store, orchestrator,  # type: ignore[arg-type]
            coordinator_factory=NullCoordinatorFactory(),
        )
        service = GraphControlService(
            instance_store, recovery, coordinator_lookup=lambda gid: None
        )
        engine = InMemoryGraphEngineController(6004)
        service.register_engine(engine)

        await service.handle(_make_resume_command(6004))

        assert len(orchestrator.calls) == 1


# -- CoordinatorFactory injection -------------------------------------------


class _RecordingCoordinatorFactory(CoordinatorFactory):
    def __init__(self) -> None:
        self.calls: list[tuple[int, GraphInstanceStore]] = []
        self._null = NullCoordinatorFactory()

    def create(
        self,
        graph_instance_id: int,
        instance_store: GraphInstanceStore,
    ) -> GraphPersistenceCoordinator:
        self.calls.append((graph_instance_id, instance_store))
        return self._null.create(graph_instance_id, instance_store)


class TestCoordinatorFactoryInjection:
    @pytest.mark.asyncio
    async def test_recover_crashed_uses_injected_factory(self) -> None:
        crashed = _make_instance(2001, status=GraphInstanceStatus.CRASHED.value)
        instance_store = InMemoryGraphInstanceStore()
        instance_store.save(crashed.metadata)
        coord_factory = _RecordingCoordinatorFactory()
        service = GraphRecoveryService(
            instance_store,
            _RecordingOrchestrator(),  # type: ignore[arg-type]
            coordinator_factory=coord_factory,
        )

        await service.recover_crashed()

        assert len(coord_factory.calls) == 1
        called_gid, called_store = coord_factory.calls[0]
        assert called_gid == 2001
        assert called_store is instance_store

    @pytest.mark.asyncio
    async def test_resume_uses_injected_factory(self) -> None:
        paused = _make_instance(2002, status=GraphInstanceStatus.PAUSED.value)
        instance_store = InMemoryGraphInstanceStore()
        instance_store.save(paused.metadata)
        coord_factory = _RecordingCoordinatorFactory()
        service = GraphRecoveryService(
            instance_store,
            _RecordingOrchestrator(),  # type: ignore[arg-type]
            coordinator_factory=coord_factory,
        )

        await service.resume(2002)

        assert len(coord_factory.calls) == 1
        called_gid, called_store = coord_factory.calls[0]
        assert called_gid == 2002
        assert called_store is instance_store
