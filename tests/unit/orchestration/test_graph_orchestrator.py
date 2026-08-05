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
- **Engine factory adapter:** the internal adapter delegates to
  ``_run_existing_instance`` for the recovery path.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

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
    GraphPersistenceCoordinator,
    GraphSpec,
    GraphState,
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

    async def test_instance_registered_after_create_and_run(self) -> None:
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_and_run(spec_id)

        assert gid in orch._active_instances
        instance = orch._active_instances[gid]
        assert instance.graph_instance_id == gid
        assert instance.spec_id == spec_id

    async def test_registered_coordinator_has_nodes_registered(self) -> None:
        """Nodes are registered on the coordinator at construction time."""
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_and_run(spec_id)

        coordinator = _get_coordinator(orch, gid)
        store = coordinator.get_deliver_store("entry")
        assert store is not None

    async def test_instance_stays_in_registry_after_completion(self) -> None:
        """Terminal status does NOT auto-evict — stays for state queries."""
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_and_run(spec_id)

        assert gid in orch._active_instances

    async def test_unregister_instance_removes_from_registry(self) -> None:
        """unregister_instance removes from _active_instances."""
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_and_run(spec_id)

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
        gid = await orch.create_and_run(spec_id)

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

        gid = await orch.create_and_run(spec_id)
        old_coordinator = _get_coordinator(orch, gid)
        instance_store.update_status(gid, GraphInstanceStatus.CRASHED)

        recovered = await orch.recover_crashed()

        assert gid in recovered
        assert gid in orch._active_instances
        new_coordinator = _get_coordinator(orch, gid)
        assert new_coordinator is not old_coordinator

    async def test_recovery_evicts_and_closes_old_coordinator(self) -> None:
        """The old coordinator's close() is called during eviction."""
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_and_run(spec_id)
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

        # Create + execute
        gid = await orch.create_and_run(spec_id)
        assert _load_status(instance_store, gid) == GraphInstanceStatus.COMPLETED.value
        assert gid in orch._active_instances

        # Simulate crash
        instance_store.update_status(gid, GraphInstanceStatus.CRASHED)

        # Recover (evicts old, registers new)
        recovered = await orch.recover_crashed()
        assert gid in recovered
        assert _load_status(instance_store, gid) == GraphInstanceStatus.COMPLETED.value
        assert gid in orch._active_instances

        # Explicit eviction
        orch.unregister_instance(gid)
        assert gid not in orch._active_instances


# -- Control delegation: pause / stop / resume / deliver -----------------


class TestControlDelegation:
    async def test_pause_sets_status_to_paused(self) -> None:
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_and_run(spec_id)

        await orch.pause(gid)

        assert _load_status(instance_store, gid) == GraphInstanceStatus.PAUSED.value

    async def test_stop_sets_status_to_stopped(self) -> None:
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_and_run(spec_id)

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

        with pytest.raises(ValueError, match="only PAUSED/STOPPED"):
            await orch.resume(gid)

    async def test_deliver_to_node_routes_through_coordinator(self) -> None:
        """Deliver routes through coordinator.route_deliver."""
        orch, spec_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_and_run(spec_id)

        await orch.deliver_to_node(gid, "entry", "hello")

        coordinator = _get_coordinator(orch, gid)
        store = coordinator.get_deliver_store("entry")
        assert store is not None
        pending = store.query_consumable(gid, "entry")
        assert len(pending) == 1
        assert pending[0].content == "hello"
        assert pending[0].source_node == "__external__"

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


# -- Engine factory adapter (recovery path) ------------------------------


class TestEngineFactoryAdapter:
    async def test_adapter_is_graph_engine_factory(self) -> None:
        from modex_agent.control.graph_recovery import GraphEngineFactory

        orch, _, _ = _make_orchestrator()
        assert isinstance(orch._engine_factory, GraphEngineFactory)

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
        assert gid in orch._active_instances

    async def test_run_existing_instance_registers_nodes_on_coordinator(self) -> None:
        """_run_existing_instance registers nodes on the coordinator."""
        orch, spec_store, instance_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_and_run(spec_id)
        metadata = instance_store.load(gid)
        assert metadata is not None
        orch.unregister_instance(gid)

        instance = GraphInstance(metadata, create_null_coordinator(gid))
        assert instance.coordinator.get_deliver_store("entry") is None

        await orch._run_existing_instance(instance)

        assert instance.coordinator.get_deliver_store("entry") is not None

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
