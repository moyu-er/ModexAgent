# ruff: noqa: ANN401
"""Tests for Node.run() coordinator-driven lifecycle.

Covers the acceptance criteria:

- begin → integrate → execute → complete/cancel/suspend/crash → finally
- Resume: previous SUPERSEDED/suspended invocation → skip re-consume
- fork() coordinator propagation (inherited by default, overridable)
- fork() current_invocation NOT inherited
"""

from __future__ import annotations

from typing import Any

import pytest
from helpers import CounterState, make_coordinator, make_runtime

from modex_graph import (
    GraphContext,
    GraphDrained,
    GraphInterrupt,
    GraphPersistenceCoordinator,
    InMemoryDeliverStoreFactory,
    IntegratedInput,
    InvocationStatus,
    MemoryGraphMetadataStore,
    Node,
    NodeResult,
    SimpleNodeStateFactory,
)


def _make_inspectable_coordinator(
    node_names: tuple[str, ...] = (),
) -> GraphPersistenceCoordinator:
    """Coordinator with SimpleNodeState + InMemoryDeliverStore for inspection."""
    coord = GraphPersistenceCoordinator(
        graph_instance_id=0,
        graph_metadata_store=MemoryGraphMetadataStore(),
        default_node_state_factory=SimpleNodeStateFactory(),
        default_deliver_store_factory=InMemoryDeliverStoreFactory(),
    )
    for name in node_names:
        coord.register_node(name)
    return coord


def _make_ctx(
    coordinator: GraphPersistenceCoordinator | None = None,
) -> GraphContext[CounterState]:
    coord = coordinator if coordinator is not None else make_coordinator()
    ctx = GraphContext(
        state=CounterState(),
        runtime=make_runtime(),
        coordinator=coord,
    )
    ctx.set_dispatch_handler(lambda _src, _tgt, _update: None)
    return ctx


# ── Test node subclasses ──────────────────────────────────────────────────


class _DeliverNode(Node[CounterState]):
    """Delivers to explicit target, returns state_update."""

    def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> NodeResult:
        self.deliver("payload", "target", ctx)
        return NodeResult(state_update={"count": 1})

    max_retry = 0


class _InterruptNode(Node[CounterState]):
    """Raises GraphInterrupt during execute."""

    def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> NodeResult:
        ctx.interrupt({"reason": "approval"})
        return NodeResult()  # unreachable

    max_retry = 0


class _CancelNode(Node[CounterState]):
    """Raises GraphDrained (GraphBubbleUp, not GraphInterrupt)."""

    def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> NodeResult:
        raise GraphDrained()

    max_retry = 0


class _CrashNode(Node[CounterState]):
    """Raises a plain Exception."""

    def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> NodeResult:
        raise ValueError("boom")

    max_retry = 0


class _RecordingNode(Node[CounterState]):
    """Records integrated_input for inspection, delivers to 'target'."""

    def __init__(self) -> None:
        self.seen_payloads: list[Any] = []

    def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> NodeResult:
        self.seen_payloads.append(integrated_input.payloads)
        self.deliver("out", "target", ctx)
        return NodeResult()

    max_retry = 0


# ── Lifecycle: begin → integrate → execute → complete ────────────────────


class TestLifecycleComplete:
    async def test_normal_execution_completes_invocation(self) -> None:
        coord = _make_inspectable_coordinator(("test_node",))
        node = _DeliverNode()
        node.name = "test_node"
        ctx = _make_ctx(coord)

        result = await node.run(ctx)

        assert result.state_update == {"count": 1}
        latest = coord.load_latest_invocation("test_node")
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED
        assert latest.state_json == {"count": 1}
        assert ctx.current_invocation is not None
        assert ctx.current_invocation.node_name == "test_node"

    async def test_begin_sets_current_invocation(self) -> None:
        coord = _make_inspectable_coordinator(("n",))
        node = _DeliverNode()
        node.name = "n"
        ctx = _make_ctx(coord)
        assert ctx.current_invocation is None

        await node.run(ctx)

        assert ctx.current_invocation is not None
        assert ctx.current_invocation.node_name == "n"


# ── Lifecycle: suspend (GraphInterrupt → state checkpoint) ──────────────────────────


class TestLifecycleSuspend:
    async def test_graph_interrupt_suspends_invocation(self) -> None:
        coord = _make_inspectable_coordinator(("suspend_node",))
        node = _InterruptNode()
        node.name = "suspend_node"
        ctx = _make_ctx(coord)

        with pytest.raises(GraphInterrupt):
            await node.run(ctx)

        latest = coord.load_latest_invocation("suspend_node")
        assert latest is not None
        assert latest.status == InvocationStatus.RUNNING
        assert latest.suspended is True
        # state_snapshot from ctx.state.checkpoint() is persisted
        assert latest.state_json != {}

    async def test_graph_interrupt_checkpoints_state(self) -> None:
        coord = _make_inspectable_coordinator(("sn",))
        node = _InterruptNode()
        node.name = "sn"
        ctx = _make_ctx(coord)
        ctx.state.count = 42

        with pytest.raises(GraphInterrupt):
            await node.run(ctx)

        latest = coord.load_latest_invocation("sn")
        assert latest is not None
        # The checkpoint should contain the count field
        assert "count" in latest.state_json


# ── Lifecycle: cancel (GraphBubbleUp, not GraphInterrupt) ────────────────


class TestLifecycleCancel:
    async def test_graph_bubble_up_cancels_invocation(self) -> None:
        coord = _make_inspectable_coordinator(("cancel_node",))
        node = _CancelNode()
        node.name = "cancel_node"
        ctx = _make_ctx(coord)

        with pytest.raises(GraphDrained):
            await node.run(ctx)

        latest = coord.load_latest_invocation("cancel_node")
        assert latest is not None
        assert latest.status == InvocationStatus.CANCELED


# ── Lifecycle: crash (Exception) ─────────────────────────────────────────


class TestLifecycleCrash:
    async def test_exception_crashes_invocation(self) -> None:
        coord = _make_inspectable_coordinator(("crash_node",))
        node = _CrashNode()
        node.name = "crash_node"
        ctx = _make_ctx(coord)

        with pytest.raises(ValueError, match="boom"):
            await node.run(ctx)

        latest = coord.load_latest_invocation("crash_node")
        assert latest is not None
        assert latest.status == InvocationStatus.CRASHED


# ── Lifecycle: finalize always called ────────────────────────────────────


class TestLifecycleFinalize:
    async def test_finalize_called_on_success(self) -> None:
        coord = _make_inspectable_coordinator(("fn",))
        node = _DeliverNode()
        node.name = "fn"
        ctx = _make_ctx(coord)

        await node.run(ctx)

        latest = coord.load_latest_invocation("fn")
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED

    async def test_finalize_called_on_crash(self) -> None:
        coord = _make_inspectable_coordinator(("fn",))
        node = _CrashNode()
        node.name = "fn"
        ctx = _make_ctx(coord)

        with pytest.raises(ValueError):
            await node.run(ctx)

        latest = coord.load_latest_invocation("fn")
        assert latest is not None
        assert latest.status == InvocationStatus.CRASHED

    async def test_integrate_failure_crashes_invocation(self) -> None:
        """If input_integrator.integrate throws, the invocation is marked
        CRASHED (not left PENDING)."""
        coord = _make_inspectable_coordinator(("intg_fail",))

        class _CrashIntegrator:
            def integrate(self, payloads: list[Any]) -> Any:
                raise RuntimeError("integrator broke")

        node = _DeliverNode()
        node.name = "intg_fail"
        node.input_integrator = _CrashIntegrator()  # type: ignore[assignment]
        ctx = _make_ctx(coord)

        with pytest.raises(RuntimeError, match="integrator broke"):
            await node.run(ctx)

        latest = coord.load_latest_invocation("intg_fail")
        assert latest is not None
        assert latest.status == InvocationStatus.CRASHED


# ── Resume: skip re-consume ──────────────────────────────────────────


class TestI16Resume:
    async def test_resume_skips_re_consume(self) -> None:
        """When the previous invocation is suspended with a state snapshot,
        Node.run() uses the snapshot as integrated input and skips
        collect_consumable_delivers (resume)."""
        coord = _make_inspectable_coordinator(("resume_node",))

        # Set up a suspended invocation (simulating a prior suspend).
        inv1 = coord.begin_invocation("resume_node")
        coord.suspend_invocation(inv1, {"resume_target": "target"})

        # Populate the deliver store with a consumable deliver.
        store = coord.get_deliver_store("resume_node")
        assert store is not None
        store.accumulate(
            graph_instance_id=0,
            target_node="resume_node",
            source_node="upstream",
            source_invocation_id=100,
            content="upstream_data",
        )

        # Run the node — should detect resume and skip consuming the deliver.
        node = _RecordingNode()
        node.name = "resume_node"
        ctx = _make_ctx(coord)

        await node.run(ctx)

        # The deliver should NOT have been consumed (still PENDING).
        consumable = coord.collect_consumable_delivers("resume_node", inv1.invocation_id)
        assert len(consumable) == 1
        assert consumable[0].status.value == "pending"

        # The node should have received the snapshot as integrated input.
        assert len(node.seen_payloads) == 1
        assert len(node.seen_payloads[0]) == 1
        assert node.seen_payloads[0][0].source_node == "__resume__"
        assert node.seen_payloads[0][0].content == {"resume_target": "target"}

        # v1 should be SUPERSEDED, v2 should be COMPLETED.
        latest = coord.load_latest_invocation("resume_node")
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED

    async def test_non_resume_consumes_delivers(self) -> None:
        """Without a prior suspended invocation, delivers are consumed normally."""
        coord = _make_inspectable_coordinator(("consumer",))

        # Populate the deliver store.
        store = coord.get_deliver_store("consumer")
        assert store is not None
        store.accumulate(
            graph_instance_id=0,
            target_node="consumer",
            source_node="producer",
            source_invocation_id=200,
            content="data_payload",
        )

        node = _RecordingNode()
        node.name = "consumer"
        ctx = _make_ctx(coord)

        await node.run(ctx)

        # The deliver should have been consumed (no longer PENDING).
        consumable = coord.collect_consumable_delivers("consumer", 0)
        assert len(consumable) == 0

        # The node should have seen the upstream payload.
        assert len(node.seen_payloads) == 1
        assert len(node.seen_payloads[0]) == 1
        assert node.seen_payloads[0][0].source_node == "producer"
        assert node.seen_payloads[0][0].content == "data_payload"


# ── fork() coordinator propagation ───────────────────────────────────────


class TestForkCoordinatorPropagation:
    def test_coordinator_inherited_by_default(self) -> None:
        coord = make_coordinator(("n",))
        ctx = _make_ctx(coord)

        child = ctx.fork(state=CounterState())

        assert child.coordinator is coord

    def test_coordinator_overridable(self) -> None:
        coord1 = make_coordinator(("n",))
        coord2 = make_coordinator(("m",))
        ctx = _make_ctx(coord1)

        child = ctx.fork(state=CounterState(), coordinator=coord2)

        assert child.coordinator is coord2

    def test_current_invocation_not_inherited(self) -> None:
        coord = make_coordinator(("n",))
        ctx = _make_ctx(coord)

        # Simulate Node.run() step 1 setting current_invocation.
        inv = coord.begin_invocation("n")
        ctx.current_invocation = inv
        assert ctx.current_invocation is not None

        child = ctx.fork(state=CounterState())

        assert child.current_invocation is None

    def test_current_invocation_settable_on_fork(self) -> None:
        coord = make_coordinator(("n",))
        ctx = _make_ctx(coord)

        inv = coord.begin_invocation("n")
        child = ctx.fork(state=CounterState(), current_invocation=inv)

        assert child.current_invocation is inv
