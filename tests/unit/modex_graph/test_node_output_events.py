# ruff: noqa: ANN401
"""Tests for node-level graph output events (graph-visualization G09).

Covers the single emission seam on ``GraphPersistenceCoordinator``:

- ``Node.run`` emits ``node_started`` → ``node_completed`` (crash path:
  ``node_started`` → ``node_crashed``) with node_id / node_name /
  invocation_id / timestamp.
- ``route_deliver`` emits ``deliver_dispatched`` with source ``node_id`` +
  ``target_node_id``.
- Emit failures are log-and-continue (graph execution is never affected).
- No adapter wired → emission is a no-op.
"""

from __future__ import annotations

import asyncio

import pytest
from helpers import CounterState, make_coordinator, make_runtime

from modex_graph import (
    GraphContext,
    GraphOutput,
    GraphOutputAdapter,
    GraphOutputKind,
    GraphPersistenceCoordinator,
    IntegratedInput,
    Node,
)


class _RecordingAdapter(GraphOutputAdapter):
    def __init__(self) -> None:
        self.outputs: list[GraphOutput] = []

    async def emit(self, output: GraphOutput) -> None:
        self.outputs.append(output)


class _FailingAdapter(GraphOutputAdapter):
    async def emit(self, output: GraphOutput) -> None:
        raise RuntimeError("emit boom")


class _DeliverNode(Node[CounterState]):
    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver("payload", "target", ctx)


class _CrashNode(Node[CounterState]):
    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        raise RuntimeError("node boom")


def _make_ctx(
    coordinator: GraphPersistenceCoordinator,
) -> GraphContext[CounterState]:
    ctx = GraphContext(
        state=CounterState(),
        runtime=make_runtime(),
        coordinator=coordinator,
    )
    ctx.set_dispatch_handler(lambda _src, _tgt: None)
    return ctx


def _set_identity(node: Node[CounterState], name: str) -> None:
    node.name = name
    node.node_id = name


async def _drain(coordinator: GraphPersistenceCoordinator) -> None:
    await coordinator.drain_output_events()


async def test_node_run_emits_started_then_completed() -> None:
    coordinator = make_coordinator()
    adapter = _RecordingAdapter()
    coordinator.set_output_adapter(adapter)
    node = _DeliverNode()
    _set_identity(node, "adder")

    await node.run(_make_ctx(coordinator))
    await _drain(coordinator)

    assert [o.kind for o in adapter.outputs] == [
        GraphOutputKind.NODE_STARTED,
        GraphOutputKind.DELIVER_DISPATCHED,
        GraphOutputKind.NODE_COMPLETED,
    ]
    for output in (adapter.outputs[0], adapter.outputs[2]):
        assert output.node_id == "adder"
        assert output.node_name == "adder"
        assert output.invocation_id is not None
        assert output.timestamp is not None
        assert output.graph_instance_id == 0
    assert (
        adapter.outputs[0].invocation_id == adapter.outputs[2].invocation_id
    )


async def test_node_crash_emits_node_crashed() -> None:
    coordinator = make_coordinator()
    adapter = _RecordingAdapter()
    coordinator.set_output_adapter(adapter)
    node = _CrashNode()
    _set_identity(node, "crasher")

    with pytest.raises(RuntimeError, match="node boom"):
        await node.run(_make_ctx(coordinator))
    await _drain(coordinator)

    assert [o.kind for o in adapter.outputs] == [
        GraphOutputKind.NODE_STARTED,
        GraphOutputKind.NODE_CRASHED,
    ]
    crashed = adapter.outputs[1]
    assert crashed.node_id == "crasher"
    assert crashed.node_name == "crasher"
    assert crashed.invocation_id is not None
    assert crashed.error == "node boom"
    assert crashed.timestamp is not None


async def test_route_deliver_emits_deliver_dispatched() -> None:
    coordinator = make_coordinator(("source", "target"))
    adapter = _RecordingAdapter()
    coordinator.set_output_adapter(adapter)

    coordinator.route_deliver(
        "target", "content", source_node_id="source", source_invocation_id=7
    )
    await _drain(coordinator)

    assert len(adapter.outputs) == 1
    output = adapter.outputs[0]
    assert output.kind is GraphOutputKind.DELIVER_DISPATCHED
    assert output.node_id == "source"
    assert output.target_node_id == "target"
    assert output.timestamp is not None
    assert output.graph_instance_id == 0


async def test_emit_failure_does_not_affect_node_run() -> None:
    coordinator = make_coordinator()
    coordinator.set_output_adapter(_FailingAdapter())
    node = _DeliverNode()
    _set_identity(node, "adder")

    await node.run(_make_ctx(coordinator))
    await _drain(coordinator)

    store = coordinator.node_state_store
    latest = store.load_latest("adder")
    assert latest is not None
    assert latest.status == "completed"


async def test_route_deliver_emit_failure_does_not_raise() -> None:
    coordinator = make_coordinator(("source", "target"))
    coordinator.set_output_adapter(_FailingAdapter())

    deliver_id = coordinator.route_deliver(
        "target", "content", source_node_id="source", source_invocation_id=1
    )
    await _drain(coordinator)

    assert deliver_id is not None


async def test_no_adapter_emission_is_noop() -> None:
    coordinator = make_coordinator()
    node = _DeliverNode()
    _set_identity(node, "adder")

    await node.run(_make_ctx(coordinator))
    coordinator.route_deliver(
        "target", "content", source_node_id="adder", source_invocation_id=1
    )


def test_emit_without_running_loop_is_noop() -> None:
    coordinator = make_coordinator(("source", "target"))
    adapter = _RecordingAdapter()
    coordinator.set_output_adapter(adapter)

    coordinator.route_deliver(
        "target", "content", source_node_id="source", source_invocation_id=1
    )

    assert adapter.outputs == []
    assert not coordinator._emit_tasks


async def test_drain_output_events_waits_for_pending_emits() -> None:
    coordinator = make_coordinator()
    gate = asyncio.Event()

    class _GatedAdapter(GraphOutputAdapter):
        def __init__(self) -> None:
            self.outputs: list[GraphOutput] = []

        async def emit(self, output: GraphOutput) -> None:
            await gate.wait()
            self.outputs.append(output)

    adapter = _GatedAdapter()
    coordinator.set_output_adapter(adapter)
    coordinator.emit_output(GraphOutputKind.NODE_STARTED, node_id="n")
    assert adapter.outputs == []

    gate.set()
    await coordinator.drain_output_events()

    assert len(adapter.outputs) == 1
