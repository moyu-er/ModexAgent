# ruff: noqa: ANN401

"""Tests for `GraphOrchestrator` (ticket 10 §3.6 / P3.5).

Covers:

- **E2E:** GraphSpec → compile → GraphInstance → GraphEngine execution.
  A simple ``FunctionNode``-based graph mutates state; the test verifies
  the state result + instance lifecycle status (COMPLETED).
- **Control delegation:** ``pause`` / ``stop`` / ``resume`` / ``deliver_to_node``
  route through ``GraphControlService`` via ``ControlCommand`` (rule 15:
  single control path). Verifies status transitions + deliver persistence.
- **Recovery:** ``recover_crashed`` delegates to ``GraphRecoveryService``
  and re-runs crashed instances.
- **Error handling:** engine exceptions set status to CRASHED;
  ``GraphInterrupt`` sets status to PAUSED.
- **Spec-not-found:** raises ``ValueError`` for unknown spec_id.
- **Engine factory adapter:** the internal adapter delegates to
  ``_run_existing_instance`` for the recovery path.
"""

from __future__ import annotations

from typing import Annotated, Any

import pytest
from pydantic import BaseModel

from modex_agent.orchestration import GraphOrchestrator
from modex_graph import (
    EdgeSpec,
    FunctionNodeFactory,
    GraphContext,
    GraphInstance,
    GraphInstanceStatus,
    GraphNode,
    GraphSpec,
    GraphState,
    InMemoryDeliverStore,
    InMemoryGraphInstanceStore,
    InMemoryGraphSpecStore,
    IntegratedInput,
    LastValue,
    MemoryCheckpointStore,
    Node,
    NodeFactory,
    NodeRegistry,
    NodeResult,
    NodeSpec,
    SimpleStateFactory,
    StateFieldSpec,
    StateRegistry,
    StateSchema,
)

# ── Test state + node fixtures ────────────────────────────────────────


class CounterState(GraphState):
    """Simple state with a counter for testing."""

    count: Annotated[int, LastValue] = 0


def _increment(ctx: GraphContext[Any]) -> None:
    """Function node body: increment ``ctx.state.count`` by 1."""
    ctx.state.count += 1


def _add_five(ctx: GraphContext[Any]) -> None:
    """Function node body: add 5 to ``ctx.state.count``."""
    ctx.state.count += 5


class _FailingNode(Node[CounterState]):
    """Node that raises a RuntimeError during execute."""

    def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> NodeResult:
        raise RuntimeError("boom")


class _FailingFactory(NodeFactory):
    """Factory that creates ``_FailingNode`` instances."""

    def create(self, spec: NodeSpec) -> Node[Any]:
        return _FailingNode()

    def config_schema(self) -> type[BaseModel] | None:
        return None


class _InterruptNode(Node[CounterState]):
    """Node that calls ``ctx.interrupt()`` to suspend the graph."""

    def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> NodeResult:
        ctx.state.count += 1
        ctx.interrupt("approval_needed")
        return NodeResult()  # unreachable — interrupt raises


class _InterruptFactory(NodeFactory):
    """Factory that creates ``_InterruptNode`` instances."""

    def create(self, spec: NodeSpec) -> Node[Any]:
        return _InterruptNode()

    def config_schema(self) -> type[BaseModel] | None:
        return None


# ── Registry + spec builders ──────────────────────────────────────────


def _function_registry() -> NodeRegistry:
    """NodeRegistry with a ``FunctionNodeFactory`` registered as ``"function"``."""
    registry = NodeRegistry()
    factory = FunctionNodeFactory({"increment": _increment, "add_five": _add_five})
    registry.register("function", factory)
    return registry


def _state_registry() -> StateRegistry:
    """StateRegistry with ``CounterState`` registered as ``"counter"``."""
    registry = StateRegistry()
    registry.register("counter", SimpleStateFactory(CounterState))
    return registry


def _inline_schema() -> StateSchema:
    """Inline StateSchema matching CounterState (for inline-schema tests)."""
    return StateSchema(
        name="counter",
        fields=[StateFieldSpec(name="count", field_type="int", default=0)],
    )


def _simple_spec(
    *,
    state_schema: StateSchema | str = "counter",
    name: str = "test_graph",
) -> GraphSpec:
    """Build a minimal GraphSpec: START → entry → END with one function node."""
    return GraphSpec(
        name=name,
        nodes=[
            NodeSpec(name="entry", node_type="function", config={"function": "increment"}),
        ],
        edges=[
            EdgeSpec(source=GraphNode.START, target="entry"),
            EdgeSpec(source="entry", target=GraphNode.END),
        ],
        state_schema=state_schema,
    )


def _make_orchestrator(
    *,
    node_registry: NodeRegistry | None = None,
    state_registry: StateRegistry | None = None,
    spec_store: InMemoryGraphSpecStore | None = None,
    instance_store: InMemoryGraphInstanceStore | None = None,
    deliver_store: InMemoryDeliverStore | None = None,
) -> tuple[
    GraphOrchestrator,
    InMemoryGraphSpecStore,
    InMemoryGraphInstanceStore,
    InMemoryDeliverStore,
]:
    """Build an orchestrator with in-memory stores + the test registries."""
    spec_store = spec_store if spec_store is not None else InMemoryGraphSpecStore()
    instance_store = instance_store if instance_store is not None else InMemoryGraphInstanceStore()
    deliver_store = deliver_store if deliver_store is not None else InMemoryDeliverStore()
    orchestrator = GraphOrchestrator(
        node_registry=node_registry if node_registry is not None else _function_registry(),
        state_registry=state_registry if state_registry is not None else _state_registry(),
        spec_store=spec_store,
        instance_store=instance_store,
        checkpoint_store=MemoryCheckpointStore(),
        deliver_store=deliver_store,
    )
    return orchestrator, spec_store, instance_store, deliver_store


def _save_spec(spec_store: InMemoryGraphSpecStore, spec: GraphSpec) -> int:
    """Save a spec and return its spec_id."""
    return spec_store.save(spec)


def _load_status(store: InMemoryGraphInstanceStore, gid: int) -> str:
    """Load instance status, asserting the instance exists."""
    instance = store.load_by_id(gid)
    assert instance is not None, f"Instance {gid} not found in store"
    return instance.status


# ── E2E: create_and_run ───────────────────────────────────────────────


class TestCreateAndRun:
    """End-to-end: GraphSpec → compile → GraphInstance → GraphEngine execution."""

    async def test_creates_instance_and_runs_graph(self) -> None:
        """Spec → compile → run. Instance saved with RUNNING, ends COMPLETED."""
        orch, spec_store, instance_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_and_run(spec_id)

        assert isinstance(gid, int)
        assert gid > 0
        assert _load_status(instance_store, gid) == GraphInstanceStatus.COMPLETED.value

    async def test_state_mutated_by_function_node(self) -> None:
        """The FunctionNode increments state.count — verified via initial_state."""
        orch, spec_store, _, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        state = CounterState(count=0)
        await orch.create_and_run(spec_id, initial_state=state)

        assert state.count == 1

    async def test_inline_state_schema_runs_to_completion(self) -> None:
        """Inline StateSchema (not a registered name) works via DynamicStateFactory."""
        orch, spec_store, instance_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec(state_schema=_inline_schema()))

        gid = await orch.create_and_run(spec_id)

        assert _load_status(instance_store, gid) == GraphInstanceStatus.COMPLETED.value

    async def test_unknown_spec_id_raises_value_error(self) -> None:
        """Loading a non-existent spec_id raises ValueError."""
        orch, _, _, _ = _make_orchestrator()

        with pytest.raises(ValueError, match="not found"):
            await orch.create_and_run(spec_id=999999)

    async def test_parent_instance_id_persisted(self) -> None:
        """parent_instance_id is saved on the GraphInstance."""
        orch, spec_store, instance_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        parent = 12345
        gid = await orch.create_and_run(spec_id, parent_instance_id=parent)

        instance = instance_store.load_by_id(gid)
        assert instance is not None
        assert instance.parent_instance_id == parent

    async def test_returns_monotonic_snowflake_ids(self) -> None:
        """The returned graph_instance_id is a positive int (Snowflake format)."""
        orch, spec_store, _, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid1 = await orch.create_and_run(spec_id)
        gid2 = await orch.create_and_run(spec_id)

        assert isinstance(gid1, int) and isinstance(gid2, int)
        assert gid2 > gid1


# ── Control delegation: pause / stop / resume / deliver ───────────────


class TestControlDelegation:
    """pause / stop / resume / deliver_to_node route through GraphControlService."""

    async def test_pause_sets_status_to_paused(self) -> None:
        """pause() → GraphControlService → instance status = PAUSED."""
        orch, spec_store, instance_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_and_run(spec_id)

        await orch.pause(gid)

        assert _load_status(instance_store, gid) == GraphInstanceStatus.PAUSED.value

    async def test_stop_sets_status_to_stopped(self) -> None:
        """stop() → GraphControlService → instance status = STOPPED."""
        orch, spec_store, instance_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_and_run(spec_id)

        await orch.stop(gid)

        assert _load_status(instance_store, gid) == GraphInstanceStatus.STOPPED.value

    async def test_resume_sets_status_to_completed(self) -> None:
        """resume() → recovery service → re-run graph → COMPLETED."""
        orch, spec_store, instance_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_and_run(spec_id)
        instance_store.update_status(gid, GraphInstanceStatus.PAUSED.value)

        await orch.resume(gid)

        assert _load_status(instance_store, gid) == GraphInstanceStatus.COMPLETED.value

    async def test_resume_rejected_for_completed_instance(self) -> None:
        """resume() on a COMPLETED instance raises (only PAUSED/STOPPED allowed)."""
        orch, spec_store, _, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_and_run(spec_id)

        with pytest.raises(ValueError, match="only PAUSED/STOPPED"):
            await orch.resume(gid)

    async def test_deliver_to_node_persists_in_deliver_store(self) -> None:
        """deliver_to_node() → DeliverStore.accumulate + engine notification."""
        orch, spec_store, _, deliver_store = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_and_run(spec_id)

        await orch.deliver_to_node(gid, "entry", "hello")

        pending = deliver_store.query_pending(gid, "entry")
        assert len(pending) == 1
        assert pending[0].content == "hello"

    async def test_deliver_to_node_unknown_instance_is_safe(self) -> None:
        """deliver_to_node() for an unknown gid still persists (no engine to notify)."""
        orch, _, _, deliver_store = _make_orchestrator()

        await orch.deliver_to_node(888888, "node", "data")

        pending = deliver_store.query_pending(888888, "node")
        assert len(pending) == 1


# ── Recovery: recover_crashed ─────────────────────────────────────────


class TestRecoverCrashed:
    """recover_crashed() delegates to GraphRecoveryService."""

    async def test_recover_crashed_re_runs_crashed_instances(self) -> None:
        """Crashed instances are auto-recovered and re-run to COMPLETED."""
        orch, spec_store, instance_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_and_run(spec_id)
        instance_store.update_status(gid, GraphInstanceStatus.CRASHED.value)

        recovered = await orch.recover_crashed()

        assert gid in recovered
        assert _load_status(instance_store, gid) == GraphInstanceStatus.COMPLETED.value

    async def test_recover_crashed_returns_empty_when_no_crashed(self) -> None:
        """No crashed instances → empty list."""
        orch, spec_store, _, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        await orch.create_and_run(spec_id)

        recovered = await orch.recover_crashed()

        assert recovered == []

    async def test_recover_crashed_skips_non_crashed_statuses(self) -> None:
        """Only CRASHED instances are auto-recovered; PAUSED/STOPPED are NOT."""
        orch, spec_store, instance_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid_completed = await orch.create_and_run(spec_id)
        gid_paused = await orch.create_and_run(spec_id)
        instance_store.update_status(gid_paused, GraphInstanceStatus.PAUSED.value)

        recovered = await orch.recover_crashed()

        assert gid_completed not in recovered
        assert gid_paused not in recovered
        assert recovered == []


# ── Error handling: lifecycle status on exceptions ────────────────────


class TestErrorHandling:
    """Engine exceptions set status to CRASHED; GraphInterrupt sets PAUSED."""

    async def test_engine_exception_sets_status_to_crashed(self) -> None:
        """A node that raises RuntimeError → instance status = CRASHED."""
        node_registry = NodeRegistry()
        node_registry.register("failing", _FailingFactory())
        orch, spec_store, instance_store, _ = _make_orchestrator(node_registry=node_registry)

        spec = GraphSpec(
            name="failing_graph",
            nodes=[NodeSpec(name="entry", node_type="failing")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target=GraphNode.END),
            ],
            state_schema="counter",
        )
        spec_id = _save_spec(spec_store, spec)

        with pytest.raises(RuntimeError, match="boom"):
            await orch.create_and_run(spec_id)

        instances = instance_store.load_by_status(GraphInstanceStatus.CRASHED.value)
        assert len(instances) == 1
        assert instances[0].spec_id == spec_id

    async def test_graph_interrupt_sets_status_to_paused(self) -> None:
        """A node that calls ctx.interrupt() → instance status = PAUSED."""
        node_registry = NodeRegistry()
        node_registry.register("interrupt", _InterruptFactory())
        orch, spec_store, instance_store, _ = _make_orchestrator(node_registry=node_registry)

        spec = GraphSpec(
            name="interrupt_graph",
            nodes=[NodeSpec(name="entry", node_type="interrupt")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target=GraphNode.END),
            ],
            state_schema="counter",
        )
        spec_id = _save_spec(spec_store, spec)

        from modex_graph import GraphInterrupt

        with pytest.raises(GraphInterrupt):
            await orch.create_and_run(spec_id)

        instances = instance_store.load_by_status(GraphInstanceStatus.PAUSED.value)
        assert len(instances) == 1
        assert instances[0].spec_id == spec_id

    async def test_engine_controller_unregistered_after_completion(self) -> None:
        """After the graph completes, the engine controller is unregistered."""
        orch, spec_store, _, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())
        gid = await orch.create_and_run(spec_id)

        assert gid not in orch._control_service._engines


# ── Engine factory adapter (recovery path) ────────────────────────────


class TestEngineFactoryAdapter:
    """The internal _EngineFactoryAdapter delegates to _run_existing_instance."""

    async def test_adapter_is_graph_engine_factory(self) -> None:
        """The orchestrator's engine factory adapter IS a GraphEngineFactory."""
        from modex_agent.control.graph_recovery import GraphEngineFactory

        orch, _, _, _ = _make_orchestrator()
        assert isinstance(orch._engine_factory, GraphEngineFactory)

    async def test_run_existing_instance_loads_spec_and_runs(self) -> None:
        """_run_existing_instance loads the spec, compiles, and runs to COMPLETED."""
        orch, spec_store, instance_store, _ = _make_orchestrator()
        spec_id = _save_spec(spec_store, _simple_spec())

        gid = await orch.create_and_run(spec_id)
        instance = instance_store.load_by_id(gid)
        assert instance is not None

        instance_store.update_status(gid, GraphInstanceStatus.CRASHED.value)
        await orch._run_existing_instance(instance)

        assert _load_status(instance_store, gid) == GraphInstanceStatus.COMPLETED.value

    async def test_run_existing_instance_unknown_spec_raises(self) -> None:
        """_run_existing_instance raises ValueError for unknown spec_id."""
        orch, _, _, _ = _make_orchestrator()

        instance = GraphInstance(graph_instance_id=1, spec_id=999999)
        with pytest.raises(ValueError, match="not found"):
            await orch._run_existing_instance(instance)


# ── Multi-node graph (integration confidence) ─────────────────────────


class TestMultiNodeGraph:
    """A multi-node graph exercises the full compile → run path."""

    async def test_two_node_chain_both_execute(self) -> None:
        """START → increment → add_five → END. State.count = 1 + 5 = 6."""
        orch, spec_store, _, _ = _make_orchestrator()

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
            state_schema="counter",
        )
        spec_id = _save_spec(spec_store, spec)

        state = CounterState(count=0)
        await orch.create_and_run(spec_id, initial_state=state)

        assert state.count == 6
