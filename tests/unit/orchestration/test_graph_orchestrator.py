# ruff: noqa: ANN401

"""Tests for `GraphOrchestrator` registry + eviction.

Covers:

- **E2E:** GraphSpec -> compile -> GraphInstance -> GraphEngine execution.
  A simple ``FunctionNode``-based graph mutates state; the test verifies
  the state result + instance lifecycle status (COMPLETED).
- **Registry:** ``_active_instances`` tracks GraphInstance
  objects; ``create_and_run`` registers; ``unregister_instance`` evicts;
  crash recovery evicts old before registering new.
- **Control delegation:** ``pause`` / ``stop`` / ``resume`` /
  ``deliver_to_node`` route through ``GraphControlService`` via
  ``ControlCommand`` (rule 15). Deliver converges on
  ``coordinator.route_deliver`` — verified via the per-node
  DeliverStore inside the coordinator.
- **Recovery:** ``recover_crashed`` delegates to ``GraphRecoveryService``
  and re-runs crashed instances.
- **Error handling:** engine exceptions set status to CRASHED;
  ``GraphInterrupt`` sets status to PAUSED.
- **Spec-not-found:** raises ``ValueError`` for unknown spec_id.
- **Recovery path:** ``_run_existing_instance`` handles eviction, spec
  compile, node_id restore, and re-registration for crashed instances.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from modex_agent.control.graph_control import LiveGraphEngineController
from modex_agent.orchestration import GraphOrchestrator
from modex_graph import (
    CoordinatorFactory,
    EdgeSpec,
    FunctionNodeFactory,
    GraphContext,
    GraphInstance,
    GraphInstanceStatus,
    GraphInstanceStore,
    GraphMetadata,
    GraphNode,
    GraphOutput,
    GraphOutputAdapter,
    GraphOutputKind,
    GraphPayload,
    GraphPersistenceCoordinator,
    GraphSpec,
    GraphState,
    GraphStateSnapshot,
    InMemoryGraphInstanceStore,
    InMemoryGraphSpecStore,
    IntegratedInput,
    Node,
    NodeFactory,
    NodeRegistry,
    NodeSpec,
    NullCoordinatorFactory,
    create_null_coordinator,
)

# -- Test state + node fixtures ------------------------------------------


class CounterState(GraphState):
    """Simple state with a counter for testing."""

    count: int = 0


def _increment(ctx: GraphContext[Any]) -> None:
    ctx.state.count += 1


def _add_five(ctx: GraphContext[Any]) -> None:
    ctx.state.count += 5


class _FailingNode(Node[CounterState]):
    """Node that raises a RuntimeError during execute."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        raise RuntimeError("boom")


class _FailingFactory(NodeFactory):
    """Factory that creates ``_FailingNode`` instances."""

    def create(self, spec: NodeSpec) -> Node[Any]:
        return _FailingNode()

    def config_schema(self) -> type[BaseModel] | None:
        return None


class _InterruptNode(Node[CounterState]):
    """Node that calls ``ctx.interrupt()`` to suspend the graph."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.count += 1
        ctx.interrupt("approval_needed")
        return None  # unreachable


class _InterruptFactory(NodeFactory):
    """Factory that creates ``_InterruptNode`` instances."""

    def create(self, spec: NodeSpec) -> Node[Any]:
        return _InterruptNode()

    def config_schema(self) -> type[BaseModel] | None:
        return None


class _BlockingNode(Node[CounterState]):
    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        self._entered = entered
        self._release = release

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self._entered.set()
        await self._release.wait()
        self.deliver(None, None, ctx)


class _BlockingFactory(NodeFactory):
    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        self._entered = entered
        self._release = release

    def create(self, spec: NodeSpec) -> Node[Any]:
        return _BlockingNode(self._entered, self._release)

    def config_schema(self) -> type[BaseModel] | None:
        return None


# -- Registry + spec builders --------------------------------------------


def _function_registry() -> NodeRegistry:
    registry = NodeRegistry()
    factory = FunctionNodeFactory({"increment": _increment, "add_five": _add_five})
    registry.register("function", factory)
    return registry


def _state_classes() -> dict[str, type[GraphState]]:
    return {"counter": CounterState}


def _simple_spec(
    *,
    state_class: str = "counter",
    name: str = "test_graph",
) -> GraphSpec:
    return GraphSpec(
        name=name,
        nodes=[
            NodeSpec(name="entry", node_type="function", config={"function": "increment"}),
        ],
        edges=[
            EdgeSpec(source=GraphNode.START, target="entry"),
            EdgeSpec(source="entry", target=GraphNode.END),
        ],
        state_class=state_class,
    )


def _make_orchestrator(
    *,
    node_registry: NodeRegistry | None = None,
    state_classes: dict[str, type[GraphState]] | None = None,
    spec_store: InMemoryGraphSpecStore | None = None,
    instance_store: InMemoryGraphInstanceStore | None = None,
) -> tuple[GraphOrchestrator, InMemoryGraphSpecStore, InMemoryGraphInstanceStore]:
    """Build an orchestrator with in-memory stores + the test registries."""
    spec_store = spec_store if spec_store is not None else InMemoryGraphSpecStore()
    instance_store = instance_store if instance_store is not None else InMemoryGraphInstanceStore()
    orchestrator = GraphOrchestrator(
        node_registry=node_registry if node_registry is not None else _function_registry(),
        state_classes=state_classes if state_classes is not None else _state_classes(),
        spec_store=spec_store,
        instance_store=instance_store,
    )
    return orchestrator, spec_store, instance_store


def _save_spec(spec_store: InMemoryGraphSpecStore, spec: GraphSpec) -> int:
    return spec_store.save(spec)


def _load_status(store: InMemoryGraphInstanceStore, gid: int) -> str:
    instance = store.load(gid)
    assert instance is not None, f"Instance {gid} not found in store"
    return instance.status


def _get_coordinator(orch: GraphOrchestrator, gid: int) -> GraphPersistenceCoordinator:
    """Get the coordinator for an active graph instance from the registry."""
    instance = orch._active_instances.get(gid)
    assert instance is not None, f"Instance {gid} not in _active_instances"
    return instance.coordinator


async def _start_blocked_graph(
    orch: GraphOrchestrator,
    spec_store: InMemoryGraphSpecStore,
    instance_store: InMemoryGraphInstanceStore,
    entered: asyncio.Event,
) -> tuple[int, asyncio.Task[int]]:
    spec = GraphSpec(
        name="externally_controlled_graph",
        nodes=[
            NodeSpec(name="entry", node_type="blocking"),
            NodeSpec(name="tail", node_type="function", config={"function": "increment"}),
        ],
        edges=[
            EdgeSpec(source=GraphNode.START, target="entry"),
            EdgeSpec(source="entry", target="tail"),
            EdgeSpec(source="tail", target=GraphNode.END),
        ],
        state_class="counter",
    )
    run_task = asyncio.create_task(orch.create_and_run(_save_spec(spec_store, spec)))
    await asyncio.wait_for(entered.wait(), timeout=5.0)
    running = instance_store.load_by_status(GraphInstanceStatus.RUNNING)
    assert len(running) == 1
    return running[0].graph_instance_id, run_task


# -- E2E: create_and_run -------------------------------------------------


class TestCreateAndRun:
    async def test_creates_instance_and_runs_graph(self) -> None:
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_and_run(spec_id)

        assert isinstance(gid, int)
        assert gid > 0
        assert _load_status(instance_store, gid) == GraphInstanceStatus.COMPLETED.value

    async def test_state_mutated_by_function_node(self) -> None:
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        state = CounterState(count=0)
        await orch.create_and_run(spec_id, initial_state=state)

        assert state.count == 1

    async def test_unknown_spec_id_raises_value_error(self) -> None:
        orch, _, _ = _make_orchestrator()

        with pytest.raises(ValueError, match="not found"):
            await orch.create_and_run(spec_id=999999)

    async def test_parent_instance_id_persisted(self) -> None:
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        parent = 12345
        gid = await orch.create_and_run(spec_id, parent_instance_id=parent)

        instance = instance_store.load(gid)
        assert instance is not None
        assert instance.parent_instance_id == parent

    async def test_returns_monotonic_snowflake_ids(self) -> None:
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid1 = await orch.create_and_run(spec_id)
        gid2 = await orch.create_and_run(spec_id)

        assert isinstance(gid1, int) and isinstance(gid2, int)
        assert gid2 > gid1


# -- Registry ------------------------------------------------


class TestRegistry:
    """``_active_instances`` registry lifecycle."""

    async def test_instance_registered_after_create_instance(self) -> None:
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_instance(spec_id)

        assert gid in orch._active_instances
        instance = orch._active_instances[gid]
        assert instance.graph_instance_id == gid
        assert instance.spec_id == spec_id

    async def test_registered_coordinator_has_nodes_registered(self) -> None:
        """Nodes are registered on the coordinator at create_instance time."""
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_instance(spec_id)

        coordinator = _get_coordinator(orch, gid)
        metadata = orch._active_instances[gid].metadata
        store = coordinator.get_deliver_store(metadata.node_id_map["entry"])
        assert store is not None

    async def test_terminal_instance_removed_after_run_instance(self) -> None:
        """M1: COMPLETED instances are auto-evicted from _active_instances."""
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_and_run(spec_id)

        assert gid not in orch._active_instances

    async def test_unregister_instance_removes_from_registry(self) -> None:
        """unregister_instance removes from _active_instances."""
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_instance(spec_id)

        assert gid in orch._active_instances
        orch.unregister_instance(gid)
        assert gid not in orch._active_instances

    async def test_unregister_instance_noop_for_unknown(self) -> None:
        """unregister on unknown gid is a safe no-op."""
        orch, _, _ = _make_orchestrator()
        orch.unregister_instance(999999)
        assert 999999 not in orch._active_instances

    async def test_unregister_instance_closes_coordinator(self) -> None:
        """unregister_instance calls coordinator.close()."""
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_instance(spec_id)

        coordinator = _get_coordinator(orch, gid)
        close_called = False
        original_close = coordinator.close

        def tracking_close() -> None:
            nonlocal close_called
            close_called = True
            original_close()

        coordinator.close = tracking_close  # type: ignore[method-assign]
        orch.unregister_instance(gid)

        assert close_called is True


# -- Eviction: crash recovery ---------------------------------------


class TestCrashRecoveryEviction:
    """Crash recovery evicts old GraphInstance before registering new."""

    async def test_recovery_evicts_old_instance(self) -> None:
        """Old GraphInstance is evicted before the new one is registered."""
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        # Use create_instance so the PENDING instance stays in the registry.
        # M1 only evicts terminal (COMPLETED/CRASHED/STOPPED) instances.
        gid = await orch.create_instance(spec_id)
        instance_store.update_status(gid, GraphInstanceStatus.CRASHED)

        recovered = await orch.recover_crashed()

        assert gid in recovered
        # After recovery + run_instance, M1 evicts the COMPLETED instance.
        assert gid not in orch._active_instances
        # The old coordinator was closed during _run_existing_instance eviction.
        new_metadata = instance_store.load(gid)
        assert new_metadata is not None
        assert new_metadata.status == GraphInstanceStatus.COMPLETED

    async def test_recovery_evicts_and_closes_old_coordinator(self) -> None:
        """The old coordinator's close() is called during eviction."""
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_instance(spec_id)
        old_coordinator = _get_coordinator(orch, gid)
        close_called = False
        original_close = old_coordinator.close

        def tracking_close() -> None:
            nonlocal close_called
            close_called = True
            original_close()

        old_coordinator.close = tracking_close  # type: ignore[method-assign]
        instance_store.update_status(gid, GraphInstanceStatus.CRASHED)

        await orch.recover_crashed()

        assert close_called is True


# -- E2E: create -> execute -> recover -> evict --------------------------


class TestE2ELifecycle:
    """Full lifecycle: create -> execute -> crash -> recover -> evict."""

    async def test_create_execute_recover_evict(self) -> None:
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        # Create + execute (M1 evicts COMPLETED from _active_instances)
        gid = await orch.create_and_run(spec_id)
        assert _load_status(instance_store, gid) == GraphInstanceStatus.COMPLETED.value
        assert gid not in orch._active_instances

        # Simulate crash
        instance_store.update_status(gid, GraphInstanceStatus.CRASHED)

        # Recover (creates fresh instance, runs, M1 evicts again)
        recovered = await orch.recover_crashed()
        assert gid in recovered
        assert _load_status(instance_store, gid) == GraphInstanceStatus.COMPLETED.value
        assert gid not in orch._active_instances


# -- Control delegation: pause / stop / resume / deliver -----------------


class TestControlDelegation:
    async def test_pause_drains_running_graph_without_overwriting_status(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        registry = _function_registry()
        registry.register("blocking", _BlockingFactory(entered, release))
        orch, spec_store, instance_store = _make_orchestrator(node_registry=registry)
        gid, run_task = await _start_blocked_graph(orch, spec_store, instance_store, entered)

        controller = orch._control_service._engines.get(gid)
        assert type(controller) is LiveGraphEngineController
        await orch.pause(gid)
        release.set()
        returned_gid = await run_task

        assert returned_gid == gid
        assert _load_status(instance_store, gid) == GraphInstanceStatus.PAUSED.value
        assert gid not in orch._control_service._engines

    async def test_stop_drains_running_graph_without_overwriting_status(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        registry = _function_registry()
        registry.register("blocking", _BlockingFactory(entered, release))
        orch, spec_store, instance_store = _make_orchestrator(node_registry=registry)
        gid, run_task = await _start_blocked_graph(orch, spec_store, instance_store, entered)

        controller = orch._control_service._engines.get(gid)
        assert type(controller) is LiveGraphEngineController
        await orch.stop(gid)
        release.set()
        returned_gid = await run_task

        assert returned_gid == gid
        assert _load_status(instance_store, gid) == GraphInstanceStatus.STOPPED.value
        assert gid not in orch._control_service._engines

    async def test_pause_sets_status_to_paused(self) -> None:
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_instance(spec_id)
        instance_store.update_status(gid, GraphInstanceStatus.RUNNING)

        await orch.pause(gid)

        assert _load_status(instance_store, gid) == GraphInstanceStatus.PAUSED.value

    async def test_stop_sets_status_to_stopped(self) -> None:
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_instance(spec_id)
        instance_store.update_status(gid, GraphInstanceStatus.RUNNING)

        await orch.stop(gid)

        assert _load_status(instance_store, gid) == GraphInstanceStatus.STOPPED.value

    async def test_resume_sets_status_to_completed(self) -> None:
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_and_run(spec_id)
        instance_store.update_status(gid, GraphInstanceStatus.PAUSED)

        await orch.resume(gid)

        assert _load_status(instance_store, gid) == GraphInstanceStatus.COMPLETED.value

    async def test_resume_rejected_for_completed_instance(self) -> None:
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_and_run(spec_id)

        with pytest.raises(ValueError, match="only PAUSED"):
            await orch.resume(gid)

    async def test_deliver_to_node_routes_through_coordinator(self) -> None:
        """Deliver routes through coordinator.route_deliver."""
        entered = asyncio.Event()
        release = asyncio.Event()
        node_registry = NodeRegistry()
        node_registry.register("blocking", _BlockingFactory(entered, release))
        node_registry.register("function", FunctionNodeFactory({"increment": _increment}))
        orch, spec_store, instance_store = _make_orchestrator(node_registry=node_registry)
        spec = GraphSpec(
            name="deliver_target_graph",
            nodes=[
                NodeSpec(name="entry", node_type="blocking"),
                NodeSpec(name="tail", node_type="function", config={"function": "increment"}),
            ],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target="tail"),
                EdgeSpec(source="tail", target=GraphNode.END),
            ],
            state_class="counter",
        )
        spec_id = _save_spec(spec_store, spec)
        gid = await orch.create_instance(spec_id)
        orch.start_run(gid)
        await asyncio.wait_for(entered.wait(), timeout=5.0)

        await orch.deliver_to_node(gid, "entry", "hello")

        coordinator = _get_coordinator(orch, gid)
        metadata = orch._active_instances[gid].metadata
        node_id = metadata.node_id_map["entry"]
        store = coordinator.get_deliver_store(node_id)
        assert store is not None
        pending = store.query_consumable(gid, node_id)
        assert len(pending) == 1
        assert pending[0].content == "hello"
        assert pending[0].source_node_id == "__external__"

        release.set()
        await asyncio.sleep(0.1)

    async def test_deliver_to_node_unknown_instance_raises(self) -> None:
        """Deliver to unknown gid raises ValueError (no active coordinator)."""
        orch, _, _ = _make_orchestrator()

        with pytest.raises(ValueError, match="No active graph instance"):
            await orch.deliver_to_node(888888, "node", "data")


# -- Recovery: recover_crashed -------------------------------------------


class TestRecoverCrashed:
    async def test_recover_crashed_re_runs_crashed_instances(self) -> None:
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_and_run(spec_id)
        instance_store.update_status(gid, GraphInstanceStatus.CRASHED)

        recovered = await orch.recover_crashed()

        assert gid in recovered
        assert _load_status(instance_store, gid) == GraphInstanceStatus.COMPLETED.value

    async def test_recover_crashed_returns_empty_when_no_crashed(self) -> None:
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        await orch.create_and_run(spec_id)

        recovered = await orch.recover_crashed()

        assert recovered == []

    async def test_recover_crashed_skips_non_crashed_statuses(self) -> None:
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid_completed = await orch.create_and_run(spec_id)
        gid_paused = await orch.create_and_run(spec_id)
        instance_store.update_status(gid_paused, GraphInstanceStatus.PAUSED)

        recovered = await orch.recover_crashed()

        assert gid_completed not in recovered
        assert gid_paused not in recovered
        assert recovered == []


# -- Error handling: lifecycle status on exceptions ----------------------


class TestErrorHandling:
    async def test_engine_exception_sets_status_to_crashed(self) -> None:
        node_registry = NodeRegistry()
        node_registry.register("failing", _FailingFactory())
        orch, spec_store, instance_store = _make_orchestrator(node_registry=node_registry)

        spec = GraphSpec(
            name="failing_graph",
            nodes=[NodeSpec(name="entry", node_type="failing")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target=GraphNode.END),
            ],
            state_class="counter",
        )
        spec_id = _save_spec(spec_store, spec)

        with pytest.raises(RuntimeError, match="boom"):
            await orch.create_and_run(spec_id)

        instances = instance_store.load_by_status(GraphInstanceStatus.CRASHED)
        assert len(instances) == 1
        assert instances[0].spec_id == spec_id
        assert instances[0].graph_instance_id not in orch._control_service._engines

    async def test_graph_interrupt_sets_status_to_paused(self) -> None:
        node_registry = NodeRegistry()
        node_registry.register("interrupt", _InterruptFactory())
        orch, spec_store, instance_store = _make_orchestrator(node_registry=node_registry)

        spec = GraphSpec(
            name="interrupt_graph",
            nodes=[NodeSpec(name="entry", node_type="interrupt")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target=GraphNode.END),
            ],
            state_class="counter",
        )
        spec_id = _save_spec(spec_store, spec)

        from modex_graph import GraphInterrupt

        with pytest.raises(GraphInterrupt):
            await orch.create_and_run(spec_id)

        instances = instance_store.load_by_status(GraphInstanceStatus.PAUSED)
        assert len(instances) == 1
        assert instances[0].spec_id == spec_id
        assert instances[0].graph_instance_id not in orch._control_service._engines

    async def test_engine_controller_unregistered_after_completion(self) -> None:
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_and_run(spec_id)

        assert gid not in orch._control_service._engines

    async def test_interrupted_instance_stays_in_registry(self) -> None:
        """GraphInterrupt keeps the instance in the registry (coordinator alive)."""
        node_registry = NodeRegistry()
        node_registry.register("interrupt", _InterruptFactory())
        orch, spec_store, _ = _make_orchestrator(node_registry=node_registry)

        spec = GraphSpec(
            name="interrupt_graph",
            nodes=[NodeSpec(name="entry", node_type="interrupt")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target=GraphNode.END),
            ],
            state_class="counter",
        )
        spec_id = _save_spec(spec_store, spec)

        from modex_graph import GraphInterrupt

        with pytest.raises(GraphInterrupt):
            await orch.create_and_run(spec_id)

        assert spec_id is not None
        gids = list(orch._active_instances.keys())
        assert len(gids) == 1
        assert gids[0] in orch._active_instances


# -- _run_existing_instance (recovery path) ------------------------------


class TestRunExistingInstance:
    async def test_run_existing_instance_loads_spec_and_runs(self) -> None:
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_and_run(spec_id)
        metadata = instance_store.load(gid)
        assert metadata is not None

        orch.unregister_instance(gid)
        instance_store.update_status(gid, GraphInstanceStatus.CRASHED)
        instance = GraphInstance(metadata, create_null_coordinator(gid))
        await orch._run_existing_instance(instance)

        assert _load_status(instance_store, gid) == GraphInstanceStatus.COMPLETED.value
        # M1: terminal instance evicted after run_instance completes
        assert gid not in orch._active_instances

    async def test_run_existing_instance_registers_nodes_on_coordinator(self) -> None:
        """_run_existing_instance registers nodes on the coordinator."""
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_and_run(spec_id)
        metadata = instance_store.load(gid)
        assert metadata is not None
        orch.unregister_instance(gid)

        instance = GraphInstance(metadata, create_null_coordinator(gid))
        entry_node_id = metadata.node_id_map["entry"]
        assert instance.coordinator.get_deliver_store(entry_node_id) is None

        await orch._run_existing_instance(instance)

        assert instance.coordinator.get_deliver_store(entry_node_id) is not None

    async def test_run_existing_instance_unknown_spec_raises(self) -> None:
        orch, _, _ = _make_orchestrator()

        instance = GraphInstance(
            GraphMetadata(
                graph_instance_id=1,
                spec_id=999999,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
            ),
            create_null_coordinator(1),
        )
        with pytest.raises(ValueError, match="not found"):
            await orch._run_existing_instance(instance)


# -- Multi-node graph (integration confidence) ---------------------------


class TestMultiNodeGraph:
    async def test_two_node_chain_both_execute(self) -> None:
        orch, spec_store, _ = _make_orchestrator()

        spec = GraphSpec(
            name="two_node_chain",
            nodes=[
                NodeSpec(name="a", node_type="function", config={"function": "increment"}),
                NodeSpec(name="b", node_type="function", config={"function": "add_five"}),
            ],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target="b"),
                EdgeSpec(source="b", target=GraphNode.END),
            ],
            state_class="counter",
        )
        spec_id = _save_spec(spec_store, spec)

        state = CounterState(count=0)
        await orch.create_and_run(spec_id, initial_state=state)

        assert state.count == 6


# -- CoordinatorFactory injection ----------------------------------------


class _RecordingFactory(CoordinatorFactory):
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
    async def test_create_and_run_uses_injected_factory(self) -> None:
        instance_store = InMemoryGraphInstanceStore()
        spec_store = InMemoryGraphSpecStore()
        factory = _RecordingFactory()
        orch = GraphOrchestrator(
            node_registry=_function_registry(),
            state_classes=_state_classes(),
            spec_store=spec_store,
            instance_store=instance_store,
            coordinator_factory=factory,
        )
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_and_run(spec_id)

        assert len(factory.calls) == 1
        called_gid, called_store = factory.calls[0]
        assert called_gid == gid
        assert called_store is instance_store

    async def test_default_factory_is_null(self) -> None:
        orch, _, _ = _make_orchestrator()
        assert isinstance(orch._coordinator_factory, NullCoordinatorFactory)

    async def test_recovery_uses_same_factory_as_create(self) -> None:
        instance_store = InMemoryGraphInstanceStore()
        spec_store = InMemoryGraphSpecStore()
        factory = _RecordingFactory()
        orch = GraphOrchestrator(
            node_registry=_function_registry(),
            state_classes=_state_classes(),
            spec_store=spec_store,
            instance_store=instance_store,
            coordinator_factory=factory,
        )
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_and_run(spec_id)
        instance_store.update_status(gid, GraphInstanceStatus.CRASHED)

        await orch.recover_crashed()

        assert len(factory.calls) == 2
        assert factory.calls[0][0] == gid
        assert factory.calls[1][0] == gid
        assert factory.calls[0][1] is instance_store
        assert factory.calls[1][1] is instance_store


# -- create_instance / run_instance split (M5/M8/M6/M1) ------------------


class TestCreateInstance:
    """``create_instance`` returns immediately (PENDING) without executing."""

    async def test_returns_immediately_with_pending_status(self) -> None:
        """M5: create_instance sets PENDING, not RUNNING."""
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_instance(spec_id)

        assert _load_status(instance_store, gid) == GraphInstanceStatus.PENDING.value
        assert gid in orch._active_instances

    async def test_does_not_execute_graph(self) -> None:
        """create_instance returns without running the engine."""
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_instance(spec_id)

        # Status remains PENDING — no engine was started
        assert _load_status(instance_store, gid) == GraphInstanceStatus.PENDING.value
        # No engine controller registered
        assert gid not in orch._control_service._engines

    async def test_stores_compiled_and_user_input_on_instance(self) -> None:
        """M8: compiled + user_input survive the create→run gap."""
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        user_input = GraphPayload(content="hello")

        gid = await orch.create_instance(spec_id, user_input=user_input)

        instance = orch._active_instances[gid]
        assert instance.compiled is not None
        assert "entry" in instance.compiled.nodes
        assert instance.user_input == user_input

    async def test_node_id_map_populated_from_compiled_nodes(self) -> None:
        """F7: node_id_map maps node names to compiled node_ids."""
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_instance(spec_id)

        instance = orch._active_instances[gid]
        assert instance.compiled is not None
        compiled_entry_id = instance.compiled.nodes["entry"].node_id
        assert instance.metadata.node_id_map["entry"] == compiled_entry_id


class TestRunInstance:
    """``run_instance`` executes a created instance."""

    async def test_executes_and_completes(self) -> None:
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_instance(spec_id)
        await orch.run_instance(gid)

        assert _load_status(instance_store, gid) == GraphInstanceStatus.COMPLETED.value

    async def test_raises_for_unknown_instance(self) -> None:
        orch, _, _ = _make_orchestrator()

        with pytest.raises(ValueError, match="not in _active_instances"):
            await orch.run_instance(999999)

    async def test_emits_graph_output_on_completion(self) -> None:
        """M6: run_instance emits GraphOutput in finally."""

        class _RecordingAdapter(GraphOutputAdapter):
            def __init__(self) -> None:
                self.outputs: list[GraphOutput] = []

            async def emit(self, output: GraphOutput) -> None:
                self.outputs.append(output)

        spec_store = InMemoryGraphSpecStore()
        adapter = _RecordingAdapter()
        orch = GraphOrchestrator(
            node_registry=_function_registry(),
            state_classes=_state_classes(),
            spec_store=spec_store,
            instance_store=InMemoryGraphInstanceStore(),
            output_adapter=adapter,
        )
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_instance(spec_id)
        await orch.run_instance(gid)

        assert len(adapter.outputs) == 1
        assert adapter.outputs[0].kind is GraphOutputKind.COMPLETED

    async def test_crashed_instance_evicted_from_registry(self) -> None:
        """M1: CRASHED instances removed from _active_instances."""
        node_registry = NodeRegistry()
        node_registry.register("failing", _FailingFactory())
        orch, spec_store, instance_store = _make_orchestrator(node_registry=node_registry)

        spec = GraphSpec(
            name="failing_graph",
            nodes=[NodeSpec(name="entry", node_type="failing")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target=GraphNode.END),
            ],
            state_class="counter",
        )
        spec_id = _save_spec(spec_store, spec)

        gid = await orch.create_instance(spec_id)
        with pytest.raises(RuntimeError, match="boom"):
            await orch.run_instance(gid)

        assert _load_status(instance_store, gid) == GraphInstanceStatus.CRASHED.value
        assert gid not in orch._active_instances

    async def test_paused_instance_retained_in_registry(self) -> None:
        """M1: PAUSED instances stay in _active_instances for resume."""
        node_registry = NodeRegistry()
        node_registry.register("interrupt", _InterruptFactory())
        orch, spec_store, instance_store = _make_orchestrator(node_registry=node_registry)

        spec = GraphSpec(
            name="interrupt_graph",
            nodes=[NodeSpec(name="entry", node_type="interrupt")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target=GraphNode.END),
            ],
            state_class="counter",
        )
        spec_id = _save_spec(spec_store, spec)

        gid = await orch.create_instance(spec_id)
        from modex_graph import GraphInterrupt

        with pytest.raises(GraphInterrupt):
            await orch.run_instance(gid)

        assert _load_status(instance_store, gid) == GraphInstanceStatus.PAUSED.value
        assert gid in orch._active_instances


# -- get_state (H7) ------------------------------------------------------


class TestGetState:
    """``get_state`` delegates to coordinator for active, store for inactive."""

    async def test_active_instance_returns_snapshot(self) -> None:
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_instance(spec_id)

        snapshot = orch.get_state(gid)

        assert isinstance(snapshot, GraphStateSnapshot)
        assert snapshot.metadata.graph_instance_id == gid
        assert snapshot.metadata.spec_id == spec_id

    async def test_inactive_instance_loads_from_store(self) -> None:
        """M1-evicted instance: get_state loads metadata from instance_store."""
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_and_run(spec_id)
        # M1 evicted the COMPLETED instance
        assert gid not in orch._active_instances

        snapshot = orch.get_state(gid)

        assert isinstance(snapshot, GraphStateSnapshot)
        assert snapshot.metadata.graph_instance_id == gid
        assert snapshot.metadata.status == GraphInstanceStatus.COMPLETED

    async def test_unknown_instance_raises(self) -> None:
        orch, _, _ = _make_orchestrator()

        with pytest.raises(ValueError, match="not found"):
            orch.get_state(999999)


# -- M4: node_id recovery ------------------------------------------------


class TestNodeIdRecovery:
    """M4: _run_existing_instance restores node_ids from node_id_map."""

    async def test_node_ids_restored_from_metadata(self) -> None:
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_instance(spec_id)
        original_node_id = orch._active_instances[gid].metadata.node_id_map["entry"]
        orch.unregister_instance(gid)
        instance_store.update_status(gid, GraphInstanceStatus.CRASHED)

        metadata = instance_store.load(gid)
        assert metadata is not None
        instance = GraphInstance(metadata, create_null_coordinator(gid))
        await orch._run_existing_instance(instance)

        # M4: compiled nodes have restored node_ids
        assert instance.compiled is not None
        restored_node_id = instance.compiled.nodes["entry"].node_id
        assert restored_node_id == original_node_id


# -- P0: Lifecycle hardening (TDD) ---------------------------------------


class _SlowNode(Node[CounterState]):
    """Node that sleeps briefly to keep the graph running."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        await asyncio.sleep(2)
        self.deliver(None, None, ctx)


class _SlowFactory(NodeFactory):
    def create(self, spec: NodeSpec) -> Node[Any]:
        return _SlowNode()

    def config_schema(self) -> type[BaseModel] | None:
        return None


class TestP0LifecycleHardening:
    """P0-1~P0-5: lifecycle guard, finalization convergence, status checks."""

    async def test_p0_1_setup_failure_writes_crashed_and_evicts(self) -> None:
        """P0-1: run_instance setup failure must write CRASHED and evict
        the instance from _active_instances.

        Triggers failure by deleting the spec from the store after
        create_instance (compile succeeds) but before run_instance
        (_load_spec inside run_instance fails).
        """
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_instance(spec_id)

        # Delete spec so _load_spec fails inside run_instance
        spec_store.delete(spec_id)

        with pytest.raises(ValueError, match="not found"):
            await orch.run_instance(gid)

        assert _load_status(instance_store, gid) == GraphInstanceStatus.CRASHED.value
        assert gid not in orch._active_instances

    async def test_p0_2_emit_failure_does_not_prevent_eviction(self) -> None:
        """P0-2: if output adapter emit raises, the instance must still be
        evicted from _active_instances.
        """

        class _FailingAdapter(GraphOutputAdapter):
            async def emit(self, output: GraphOutput) -> None:
                raise RuntimeError("adapter broken")

        spec_store = InMemoryGraphSpecStore()
        adapter = _FailingAdapter()
        orch = GraphOrchestrator(
            node_registry=_function_registry(),
            state_classes=_state_classes(),
            spec_store=spec_store,
            instance_store=InMemoryGraphInstanceStore(),
            output_adapter=adapter,
        )
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_instance(spec_id)

        await orch.run_instance(gid)

        meta = orch._instance_store.load(gid)
        assert meta is not None
        assert meta.status is GraphInstanceStatus.COMPLETED
        assert gid not in orch._active_instances

    async def test_p0_3_stop_paused_instance_evicts_from_registry(self) -> None:
        """P0-3: stopping a PAUSED instance (no engine running) must evict
        it from _active_instances and close the coordinator.
        """
        node_registry = NodeRegistry()
        node_registry.register("interrupt", _InterruptFactory())
        orch, spec_store, instance_store = _make_orchestrator(node_registry=node_registry)
        spec = GraphSpec(
            name="interrupt_graph",
            nodes=[NodeSpec(name="entry", node_type="interrupt")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target=GraphNode.END),
            ],
            state_class="counter",
        )
        spec_id = _save_spec(spec_store, spec)
        gid = await orch.create_instance(spec_id)
        with pytest.raises(Exception, match="approval_needed"):
            await orch.run_instance(gid)

        assert _load_status(instance_store, gid) == GraphInstanceStatus.PAUSED.value
        assert gid in orch._active_instances

        await orch.stop(gid)

        assert _load_status(instance_store, gid) == GraphInstanceStatus.STOPPED.value
        assert gid not in orch._active_instances

    async def test_p0_4_duplicate_start_run_raises(self) -> None:
        """P0-4: calling start_run on an already-running instance must
        raise ValueError instead of allowing concurrent execution.
        """
        node_registry = NodeRegistry()
        node_registry.register("slow", _SlowFactory())
        orch, spec_store, instance_store = _make_orchestrator(node_registry=node_registry)
        spec = GraphSpec(
            name="slow_graph",
            nodes=[NodeSpec(name="entry", node_type="slow")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target=GraphNode.END),
            ],
            state_class="counter",
        )
        spec_id = _save_spec(spec_store, spec)
        gid = await orch.create_instance(spec_id)
        orch.start_run(gid)
        await asyncio.sleep(0.2)

        with pytest.raises(ValueError, match="already running"):
            await orch.run_instance(gid)

        await asyncio.sleep(3)

    async def test_p0_5_deliver_to_pending_instance_rejected(self) -> None:
        """P0-5: deliver_to_node on a PENDING instance (not yet RUNNING)
        must be rejected with ValueError.
        """
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_instance(spec_id)

        assert _load_status(instance_store, gid) == GraphInstanceStatus.PENDING.value

        with pytest.raises(ValueError, match="must be RUNNING or PAUSED"):
            await orch.deliver_to_node(gid, "entry", "test content")


# -- P1: Lifecycle hardening (TDD) --------------------------------------


class TestP1LifecycleHardening:
    """P1-2/P1-4/P1-5: turn lifecycle convergence, cancellation, session cleanup."""

    async def test_p1_4_cancellederror_writes_stopped_not_running(self) -> None:
        """P1-4: when run_instance is cancelled (workspace eviction),
        status must be STOPPED, not left as RUNNING.

        Without the fix, CancelledError bypasses except Exception,
        finally runs with status=RUNNING, _finalize_instance does not
        evict (RUNNING is not terminal), instance leaks.
        """
        node_registry = NodeRegistry()
        node_registry.register("slow", _SlowFactory())
        orch, spec_store, instance_store = _make_orchestrator(node_registry=node_registry)
        spec = GraphSpec(
            name="slow_graph",
            nodes=[NodeSpec(name="entry", node_type="slow")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target=GraphNode.END),
            ],
            state_class="counter",
        )
        spec_id = _save_spec(spec_store, spec)
        gid = await orch.create_instance(spec_id)
        orch.start_run(gid)
        await asyncio.sleep(0.2)

        # Cancel the running task (simulates workspace eviction)
        task = next(t for t in orch._run_tasks if not t.done())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # P1-4 assertions: status STOPPED, instance evicted
        assert _load_status(instance_store, gid) == GraphInstanceStatus.STOPPED.value
        assert gid not in orch._active_instances
