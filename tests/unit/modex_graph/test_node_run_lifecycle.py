# ruff: noqa: ANN401
"""Tests for Node.run() coordinator-driven lifecycle.

Covers:

- begin → integrate → execute → complete/cancel/suspend/crash → finally
- Resume: previous suspended invocation → skip re-consume
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
    InMemoryGraphInstanceStore,
    InMemoryNodeStateStore,
    IntegratedInput,
    InvocationStatus,
    Node,
)


def _make_inspectable_coordinator(
    node_names: tuple[str, ...] = (),
) -> GraphPersistenceCoordinator:
    """Coordinator with InMemoryNodeStateStore + InMemoryDeliverStore for inspection."""
    coord = GraphPersistenceCoordinator(
        graph_instance_id=0,
        instance_store=InMemoryGraphInstanceStore(),
        node_state_store=InMemoryNodeStateStore(0),
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


class _DeliverNode(Node[CounterState]):
    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.count += 1
        self.deliver("payload", "target", ctx)
        return None

    max_retry = 0


class _InterruptNode(Node[CounterState]):
    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        ctx.interrupt({"reason": "approval"})
        return None

    max_retry = 0


class _CancelNode(Node[CounterState]):
    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        raise GraphDrained()

    max_retry = 0


class _CrashNode(Node[CounterState]):
    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        raise ValueError("boom")

    max_retry = 0


class _RecordingNode(Node[CounterState]):
    def __init__(self) -> None:
        self.seen_payloads: list[Any] = []

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.seen_payloads.append(integrated_input.payloads)
        self.deliver("out", "target", ctx)
        return None

    max_retry = 0


class TestLifecycleComplete:
    async def test_normal_execution_completes_invocation(self) -> None:
        coord = _make_inspectable_coordinator(("test_node",))
        node = _DeliverNode()
        node.name = "test_node"
        ctx = _make_ctx(coord)

        result = await node.run(ctx)

        assert result is None
        latest = coord.node_state_store.load_latest("test_node")
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED
        assert latest.state_json == ctx.state.model_dump(mode="json")
        assert latest.state_json["count"] == 1
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


class TestLifecycleSuspend:
    async def test_graph_interrupt_suspends_invocation(self) -> None:
        coord = _make_inspectable_coordinator(("suspend_node",))
        node = _InterruptNode()
        node.name = "suspend_node"
        ctx = _make_ctx(coord)

        with pytest.raises(GraphInterrupt):
            await node.run(ctx)

        latest = coord.node_state_store.load_latest("suspend_node")
        assert latest is not None
        assert latest.status == InvocationStatus.RUNNING
        assert latest.suspended is True
        assert latest.state_json != {}

    async def test_graph_interrupt_checkpoints_state(self) -> None:
        coord = _make_inspectable_coordinator(("sn",))
        node = _InterruptNode()
        node.name = "sn"
        ctx = _make_ctx(coord)
        ctx.state.count = 42

        with pytest.raises(GraphInterrupt):
            await node.run(ctx)

        latest = coord.node_state_store.load_latest("sn")
        assert latest is not None
        assert "count" in latest.state_json


class TestLifecycleCancel:
    async def test_graph_bubble_up_cancels_invocation(self) -> None:
        coord = _make_inspectable_coordinator(("cancel_node",))
        node = _CancelNode()
        node.name = "cancel_node"
        ctx = _make_ctx(coord)

        with pytest.raises(GraphDrained):
            await node.run(ctx)

        latest = coord.node_state_store.load_latest("cancel_node")
        assert latest is not None
        assert latest.status == InvocationStatus.CANCELED


class TestLifecycleCrash:
    async def test_exception_crashes_invocation(self) -> None:
        coord = _make_inspectable_coordinator(("crash_node",))
        node = _CrashNode()
        node.name = "crash_node"
        ctx = _make_ctx(coord)

        with pytest.raises(ValueError, match="boom"):
            await node.run(ctx)

        latest = coord.node_state_store.load_latest("crash_node")
        assert latest is not None
        assert latest.status == InvocationStatus.CRASHED


class TestLifecycleFinalize:
    async def test_finalize_called_on_success(self) -> None:
        coord = _make_inspectable_coordinator(("fn",))
        node = _DeliverNode()
        node.name = "fn"
        ctx = _make_ctx(coord)

        await node.run(ctx)

        latest = coord.node_state_store.load_latest("fn")
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED

    async def test_finalize_called_on_crash(self) -> None:
        coord = _make_inspectable_coordinator(("fn",))
        node = _CrashNode()
        node.name = "fn"
        ctx = _make_ctx(coord)

        with pytest.raises(ValueError):
            await node.run(ctx)

        latest = coord.node_state_store.load_latest("fn")
        assert latest is not None
        assert latest.status == InvocationStatus.CRASHED

    async def test_integrate_failure_crashes_invocation(self) -> None:
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

        latest = coord.node_state_store.load_latest("intg_fail")
        assert latest is not None
        assert latest.status == InvocationStatus.CRASHED


class TestI16Resume:
    async def test_resume_skips_re_consume(self) -> None:
        coord = _make_inspectable_coordinator(("resume_node",))
        store = coord.node_state_store

        inv1 = store.begin_invocation("resume_node")
        store.suspend_invocation(inv1, {"resume_target": "target"})

        deliver_store = coord.get_deliver_store("resume_node")
        assert deliver_store is not None
        deliver_store.accumulate(
            graph_instance_id=0,
            target_node="resume_node",
            source_node="upstream",
            source_invocation_id=100,
            content="upstream_data",
        )

        node = _RecordingNode()
        node.name = "resume_node"
        ctx = _make_ctx(coord)

        await node.run(ctx)

        consumable = coord.collect_consumable_delivers("resume_node", inv1.invocation_id)
        assert len(consumable) == 0

        assert len(node.seen_payloads) == 1
        assert len(node.seen_payloads[0]) == 2
        assert node.seen_payloads[0][0].source_node == "__resume__"
        assert node.seen_payloads[0][0].content == {"resume_target": "target"}
        assert node.seen_payloads[0][1].source_node == "upstream"
        assert node.seen_payloads[0][1].content == "upstream_data"

        latest = store.load_latest("resume_node")
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED

    async def test_non_resume_consumes_delivers(self) -> None:
        coord = _make_inspectable_coordinator(("consumer",))

        deliver_store = coord.get_deliver_store("consumer")
        assert deliver_store is not None
        deliver_store.accumulate(
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

        consumable = coord.collect_consumable_delivers("consumer", 0)
        assert len(consumable) == 0

        assert len(node.seen_payloads) == 1
        assert len(node.seen_payloads[0]) == 1
        assert node.seen_payloads[0][0].source_node == "producer"
        assert node.seen_payloads[0][0].content == "data_payload"


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

        inv = coord.node_state_store.begin_invocation("n")
        ctx.current_invocation = inv
        assert ctx.current_invocation is not None

        child = ctx.fork(state=CounterState())
        assert child.current_invocation is None

    def test_current_invocation_settable_on_fork(self) -> None:
        coord = make_coordinator(("n",))
        ctx = _make_ctx(coord)

        inv = coord.node_state_store.begin_invocation("n")
        child = ctx.fork(state=CounterState(), current_invocation=inv)
        assert child.current_invocation is inv
