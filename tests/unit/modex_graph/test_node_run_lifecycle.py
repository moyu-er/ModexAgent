# ruff: noqa: ANN401
"""Tests for Node.run() coordinator-driven lifecycle.

Covers:

- begin → integrate → execute → complete/cancel/crash → finally
"""

from __future__ import annotations

from typing import Any

import pytest
from helpers import CounterState, make_coordinator, make_runtime

from modex_graph import (
    DeliverConsumptionStatus,
    Graph,
    GraphContext,
    GraphDrained,
    GraphInterrupt,
    GraphNode,
    GraphPersistenceCoordinator,
    InMemoryDeliverStoreFactory,
    InMemoryGraphInstanceStore,
    InMemoryNodeStateStore,
    IntegratedInput,
    InvocationStatus,
    Node,
    RoutingError,
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
    if coord.get_deliver_store("target") is None:
        coord.register_node("target")
    ctx = GraphContext(
        state=CounterState(),
        runtime=make_runtime(),
        coordinator=coord,
    )
    ctx.set_dispatch_handler(lambda _src, _tgt: None)
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


class _LifecycleOrderNode(Node[CounterState]):
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self._events.append("execute")
        self.deliver("payload", "target", ctx)
        self._events.append("deliver")


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

    async def test_complete_promotes_staged_before_dispatch_and_input_promotion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[str] = []
        coord = _make_inspectable_coordinator(("source", "target"))
        node = _LifecycleOrderNode(events)
        _set_identity(node, "source")
        ctx = _make_ctx(coord)
        store = coord.node_state_store
        complete_invocation = store.complete_invocation
        promote_staged = coord.promote_staged_by_source
        promote_delivers = coord.promote_delivers

        def record_complete(*args: Any, **kwargs: Any) -> None:
            events.append("complete")
            complete_invocation(*args, **kwargs)

        def record_promote_staged(graph_instance_id: int, source_node_id: str) -> set[str]:
            events.append("promote_staged")
            return promote_staged(graph_instance_id, source_node_id)

        def record_promote_delivers(node_id: str, invocation_id: int) -> None:
            events.append("promote_delivers")
            promote_delivers(node_id, invocation_id)

        monkeypatch.setattr(store, "complete_invocation", record_complete)
        monkeypatch.setattr(coord, "promote_staged_by_source", record_promote_staged)
        monkeypatch.setattr(coord, "promote_delivers", record_promote_delivers)
        ctx.set_dispatch_handler(lambda _src, _target: events.append("dispatch"))

        await node.run(ctx)

        assert events == [
            "execute",
            "deliver",
            "complete",
            "promote_staged",
            "dispatch",
            "promote_delivers",
        ]
        target_store = coord.get_deliver_store("target")
        assert target_store is not None
        records = target_store.query_consumable(0, "target")
        assert [record.content for record in records] == ["payload"]

    async def test_unknown_promoted_target_id_raises_routing_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        node = _DeliverNode()
        target = _DeliverNode()
        graph: Graph[CounterState] = Graph()
        graph.add_node("source", node)
        graph.add_node("target", target)
        graph.add_edge(GraphNode.START, "source")
        graph.add_edge("source", "target")
        graph.add_edge("target", GraphNode.END)
        compiled = graph.compile()
        coord = _make_inspectable_coordinator(
            (compiled.nodes["source"].node_id, compiled.nodes["target"].node_id)
        )
        ctx = _make_ctx(coord)
        monkeypatch.setattr(
            coord,
            "promote_staged_by_source",
            lambda _graph_instance_id, _source_node_id: {"missing-target-id"},
        )

        with pytest.raises(RoutingError, match="missing-target-id"):
            await node.run(ctx, graph=compiled)


class TestLifecycleInterrupt:
    async def test_graph_interrupt_cancels_invocation(self) -> None:
        coord = _make_inspectable_coordinator(("interrupt_node",))
        node = _InterruptNode()
        _set_identity(node, "interrupt_node")
        ctx = _make_ctx(coord)

        with pytest.raises(GraphInterrupt):
            await node.run(ctx)

        latest = coord.node_state_store.load_latest("interrupt_node")
        assert latest is not None
        assert latest.status == InvocationStatus.CANCELED


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
        assert node.seen_payloads[0][0].status == DeliverConsumptionStatus.PENDING.value
        assert node.seen_payloads[0][0].consumed_by_invocation_id is None
