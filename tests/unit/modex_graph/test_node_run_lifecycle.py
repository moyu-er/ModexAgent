# ruff: noqa: ANN401
"""Tests for Node.run() coordinator-driven lifecycle.

Covers:

- begin → integrate → execute → complete/cancel/suspend/crash → finally
- Resume: previous suspended invocation → skip re-consume
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


def _set_identity(
    node: Node[CounterState],
    name: str,
    *,
    node_id: str | None = None,
) -> None:
    node.name = name
    node.node_id = node_id if node_id is not None else name


class _DeliverNode(Node[CounterState]):
    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.count += 1
        self.deliver("payload", "target", ctx)
        return None


class _InterruptNode(Node[CounterState]):
    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        ctx.interrupt({"reason": "approval"})
        return None


class _CancelNode(Node[CounterState]):
    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        raise GraphDrained()


class _CrashNode(Node[CounterState]):
    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        raise ValueError("boom")


class _RecordingNode(Node[CounterState]):
    def __init__(self) -> None:
        self.seen_payloads: list[Any] = []

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.seen_payloads.append(integrated_input.payloads)
        self.deliver("out", "target", ctx)
        return None


class TestLifecycleComplete:
    async def test_normal_execution_completes_invocation(self) -> None:
        node_id = "node_persistence_identity"
        coord = _make_inspectable_coordinator((node_id,))
        node = _DeliverNode()
        _set_identity(node, "human_readable_name", node_id=node_id)
        ctx = _make_ctx(coord)

        result = await node.run(ctx)

        assert result is None
        latest = coord.node_state_store.load_latest(node_id)
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED
        assert latest.state_json == ctx.state.model_dump(mode="json")
        assert latest.state_json["count"] == 1
        assert coord.node_state_store.load_latest(node.name) is None
        assert latest.node_id == node_id

    async def test_begin_sets_current_invocation(self) -> None:
        coord = _make_inspectable_coordinator(("n",))
        node = _DeliverNode()
        _set_identity(node, "n")
        ctx = _make_ctx(coord)

        await node.run(ctx)

        latest = coord.node_state_store.load_latest("n")
        assert latest is not None
        assert latest.node_id == "n"
        assert latest.status == InvocationStatus.COMPLETED


class TestLifecycleSuspend:
    async def test_graph_interrupt_suspends_invocation(self) -> None:
        coord = _make_inspectable_coordinator(("suspend_node",))
        node = _InterruptNode()
        _set_identity(node, "suspend_node")
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
        _set_identity(node, "sn")
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
        _set_identity(node, "cancel_node")
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
        _set_identity(node, "crash_node")
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
        _set_identity(node, "fn")
        ctx = _make_ctx(coord)

        await node.run(ctx)

        latest = coord.node_state_store.load_latest("fn")
        assert latest is not None
        assert latest.status == InvocationStatus.COMPLETED

    async def test_finalize_called_on_crash(self) -> None:
        coord = _make_inspectable_coordinator(("fn",))
        node = _CrashNode()
        _set_identity(node, "fn")
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
        _set_identity(node, "intg_fail")
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
            node_id="resume_node",
            source_node_id="upstream",
            source_invocation_id=100,
            content="upstream_data",
        )

        node = _RecordingNode()
        _set_identity(node, "resume_node")
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
        node_id = "node_consumer_identity"
        coord = _make_inspectable_coordinator((node_id,))

        deliver_store = coord.get_deliver_store(node_id)
        assert deliver_store is not None
        deliver_store.accumulate(
            graph_instance_id=0,
            node_id=node_id,
            source_node_id="producer",
            source_invocation_id=200,
            content="data_payload",
        )

        node = _RecordingNode()
        _set_identity(node, "consumer", node_id=node_id)
        ctx = _make_ctx(coord)

        await node.run(ctx)

        consumable = coord.collect_consumable_delivers(node_id, 0)
        assert len(consumable) == 0

        assert len(node.seen_payloads) == 1
        assert len(node.seen_payloads[0]) == 1
        assert node.seen_payloads[0][0].source_node == "producer"
        assert node.seen_payloads[0][0].content == "data_payload"
