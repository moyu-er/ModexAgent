# ruff: noqa: ANN401
"""Tests for Node._execute / _deliver / deliver / _submit / submit.

Covers the additive deliver/submit dual-method API on `Node`:

- `run`: orchestrate integrate -> execute -> submit and return None.
- `deliver`: node-facing API, called during `execute()`, accumulates via
  `_deliver`.
- `_deliver`: framework-fixed accumulation (in-memory or DeliverStore-backed).
- `_submit`: framework-fixed grouping by `next_node` + dispatch.
- `submit`: node-facing customization point (default delegates to `_submit`).
- `input_integrator` / `deliver_store` attributes and defaults.
- Sync and async `execute` overrides.
- Custom `InputIntegrator`, custom `deliver`, custom `submit` overrides.
- DeliverStore-backed persistence (InMemory + graph_instance_id on ctx).
- `next_node=None` resolution raises `NotImplementedError` (additive limitation).
- `deliver()` outside `_execute` raises `RuntimeError`.

These tests call `_execute` directly — the scheduler is NOT involved
(additive step). The scheduler still calls `node.execute(ctx)` directly.
"""

from __future__ import annotations

from typing import Any

import pytest
from helpers import CounterState, make_coordinator, make_ctx, make_runtime

from modex_graph import (
    DefaultInputIntegrator,
    DeliverStore,
    GraphContext,
    GraphNode,
    InMemoryDeliverStore,
    InputIntegrator,
    IntegratedInput,
    IntegratedPayload,
    Node,
    RoutingError,
    SchedulerKind,
)

# ── Test node subclasses ──────────────────────────────────────────────────


class _NoDeliverNode(Node[CounterState]):
    """Node that does not call deliver. _submit_result should be empty."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        ctx.state.count += 1
        return None


class _SingleDeliverNode(Node[CounterState]):
    """Node that delivers once to an explicit next_node."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver("payload_a", "downstream_a", ctx)
        return None


class _MultiDeliverNode(Node[CounterState]):
    """Node that delivers multiple times to different next_nodes."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver("data_1", "target_x", ctx)
        self.deliver("data_2", "target_x", ctx)
        self.deliver("data_3", "target_y", ctx)
        return None


class _AsyncDeliverNode(Node[CounterState]):
    """Async node that delivers during async execute."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver("async_data", "async_target", ctx)
        return None


class _DeliverWithCtxNode(Node[CounterState]):
    """Node that passes ctx explicitly to deliver()."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver("explicit_ctx_data", "explicit_target", ctx)
        return None


class _ReadIntegratedInputNode(Node[CounterState]):
    """Node that reads integrated_input during execute."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        if integrated_input.integrated_content is not None:
            ctx.state.name = str(integrated_input.integrated_content)
        self.deliver("read", "read_target", ctx)
        return None


class _CustomDeliverNode(Node[CounterState]):
    """Node that overrides deliver() to add a prefix."""

    def deliver(
        self,
        content: Any,
        next_node: str | None,
        ctx: GraphContext[CounterState],
    ) -> None:
        super().deliver(f"custom:{content}", next_node, ctx)

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver("data", "target", ctx)
        return None


class _CustomSubmitNode(Node[CounterState]):
    """Node that overrides submit() to store a custom result."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver("data", "target", ctx)
        return None

    def submit(self, ctx: GraphContext[CounterState]) -> None:
        self._submit_result = {"custom_submit": ["overridden"]}


class _StoreBackedNode(Node[CounterState]):
    """Node configured with a DeliverStore. Used for persistence tests."""

    def __init__(self, store: DeliverStore) -> None:
        self.name = "store_node"
        self.deliver_store = store

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver({"key": "value"}, "db_target", ctx)
        self.deliver("second", "db_target", ctx)
        return None


class _StoreBackedAsyncNode(Node[CounterState]):
    """Async node configured with a DeliverStore."""

    def __init__(self, store: DeliverStore) -> None:
        self.name = "async_store_node"
        self.deliver_store = store

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver("async_db_data", "async_db_target", ctx)
        return None


# ── Custom InputIntegrator for testing ────────────────────────────────────


class _JoinIntegrator(InputIntegrator):
    """Joins string contents with a separator."""

    def integrate(self, payloads: list[IntegratedPayload]) -> IntegratedInput:
        joined = " + ".join(str(p.content) for p in payloads) if payloads else ""
        return IntegratedInput(payloads=payloads, integrated_content=joined)


# ── Parallel ctx helper ───────────────────────────────────────────────────


def _make_parallel_ctx(
    state: CounterState | None = None,
) -> tuple[GraphContext[CounterState], list[tuple[str, str, dict[str, Any] | None]]]:
    """Build a PARALLEL ctx with a recording dispatch handler.

    Returns (ctx, dispatch_calls) where dispatch_calls records each
    (source_instance, target, state_update) triple passed to the handler.
    """
    state = state if state is not None else CounterState()
    ctx: GraphContext[CounterState] = GraphContext(
        state=state,
        runtime=make_runtime(),
        coordinator=make_coordinator(),
        scheduler_kind=SchedulerKind.PARALLEL,
    )
    dispatch_calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(
        source_instance: str,
        target: str,
        payload: dict[str, Any] | None,
    ) -> None:
        dispatch_calls.append((source_instance, target, payload))

    ctx.set_dispatch_handler(handler)
    return ctx, dispatch_calls


def _make_linear_ctx(state: CounterState | None = None) -> GraphContext[CounterState]:
    return make_ctx(state)


# ── Node attributes ───────────────────────────────────────────────────────


class TestNodeAttributes:
    def test_input_integrator_defaults_to_default(self) -> None:
        node = _NoDeliverNode()
        assert isinstance(node.input_integrator, DefaultInputIntegrator)

    def test_input_integrator_can_be_overridden(self) -> None:
        node = _NoDeliverNode()
        custom = _JoinIntegrator()
        node.input_integrator = custom
        assert node.input_integrator is custom


# ── _execute: basic orchestration ─────────────────────────────────────────


class TestExecuteBasic:
    async def test_execute_calls_execute_and_returns_none(self) -> None:
        node = _SingleDeliverNode()
        node.name = "single_deliver"
        ctx = _make_linear_ctx(CounterState(count=0))
        result = await node.run(ctx)
        assert result is None
        assert "downstream_a" in node._submit_result

    async def test_execute_resets_pending_delivers(self) -> None:
        node = _SingleDeliverNode()
        node.name = "single_deliver"
        ctx = _make_linear_ctx()
        await node.run(ctx)
        # After _execute, _submit_result should have the deliver.
        assert "downstream_a" in node._submit_result

    async def test_execute_with_upstream_payloads(self) -> None:
        node = _ReadIntegratedInputNode()
        node.name = "read_integrated"
        node.node_id = "node-read-integrated"
        ctx = _make_linear_ctx(CounterState(name=""))
        ctx.coordinator.register_node(node.node_id)
        store = ctx.coordinator.get_deliver_store(node.node_id)
        assert store is not None
        store.accumulate(
            graph_instance_id=0,
            node_id=node.node_id,
            source_node_id="up_a",
            source_invocation_id=1,
            content="hello",
        )
        store.accumulate(
            graph_instance_id=0,
            node_id=node.node_id,
            source_node_id="up_b",
            source_invocation_id=2,
            content="world",
        )
        await node.run(ctx)
        assert ctx.state.name == "['hello', 'world']"

    async def test_execute_with_no_upstream_payloads(self) -> None:
        node = _ReadIntegratedInputNode()
        node.name = "read_integrated"
        node.node_id = "node-read-integrated"
        ctx = _make_linear_ctx(CounterState(name="initial"))
        await node.run(ctx)
        # Default integrator on empty list -> integrated_content = []
        assert ctx.state.name == "[]"

    async def test_execute_with_custom_integrator(self) -> None:
        node = _ReadIntegratedInputNode()
        node.name = "read_integrated"
        node.input_integrator = _JoinIntegrator()
        ctx = _make_linear_ctx(CounterState(name=""))
        ctx.coordinator.register_node(node.node_id)
        store = ctx.coordinator.get_deliver_store(node.node_id)
        assert store is not None
        store.accumulate(
            graph_instance_id=0,
            node_id=node.node_id,
            source_node_id="up_a",
            source_invocation_id=1,
            content="hello",
        )
        store.accumulate(
            graph_instance_id=0,
            node_id=node.node_id,
            source_node_id="up_b",
            source_invocation_id=2,
            content="world",
        )
        await node.run(ctx)
        assert ctx.state.name == "hello + world"

    async def test_execute_with_async_node_returns_none(self) -> None:
        node = _AsyncDeliverNode()
        node.name = "async_deliver"
        ctx = _make_linear_ctx()
        result = await node.run(ctx)
        assert result is None
        assert "async_target" in node._submit_result


# ── deliver: accumulation ─────────────────────────────────────────────────


class TestDeliverAccumulation:
    async def test_single_deliver_in_memory(self) -> None:
        node = _SingleDeliverNode()
        node.name = "single_deliver"
        ctx = _make_linear_ctx()
        await node.run(ctx)
        assert node._submit_result == {"downstream_a": ["payload_a"]}

    async def test_multiple_delivers_same_next_node(self) -> None:
        node = _MultiDeliverNode()
        node.name = "multi_deliver"
        ctx = _make_linear_ctx()
        await node.run(ctx)
        assert "target_x" in node._submit_result
        assert "target_y" in node._submit_result
        assert node._submit_result["target_x"] == ["data_1", "data_2"]
        assert node._submit_result["target_y"] == ["data_3"]

    async def test_deliver_with_explicit_ctx(self) -> None:
        node = _DeliverWithCtxNode()
        node.name = "explicit_ctx"
        ctx = _make_linear_ctx()
        await node.run(ctx)
        assert node._submit_result == {"explicit_target": ["explicit_ctx_data"]}

    async def test_deliver_with_explicit_ctx_outside_execute(self) -> None:
        node = _SingleDeliverNode()
        node.name = "single_deliver"
        ctx = _make_linear_ctx()
        # Passing ctx explicitly should work even outside _execute.
        node._pending_delivers = []
        node.deliver("data", "target", ctx)
        assert node._pending_delivers == [("data", "target")]

    async def test_custom_deliver_override(self) -> None:
        node = _CustomDeliverNode()
        node.name = "custom_deliver"
        ctx = _make_linear_ctx()
        await node.run(ctx)
        assert node._submit_result == {"target": ["custom:data"]}


# ── _submit: grouping and dispatch ────────────────────────────────────────


class TestSubmitGrouping:
    async def test_submit_groups_by_next_node(self) -> None:
        node = _MultiDeliverNode()
        node.name = "multi_deliver"
        ctx = _make_linear_ctx()
        await node.run(ctx)
        assert set(node._submit_result.keys()) == {"target_x", "target_y"}

    async def test_submit_under_linear_stores_result(self) -> None:
        node = _SingleDeliverNode()
        node.name = "single_deliver"
        ctx = _make_linear_ctx()
        await node.run(ctx)
        # Under LINEAR, _submit_result is set AND ctx.dispatch is called
        # (rule 15 convergence: no scheduler_kind branch). make_ctx
        # provides a no-op handler so dispatch succeeds.
        assert node._submit_result == {"downstream_a": ["payload_a"]}

    async def test_submit_under_parallel_calls_dispatch(self) -> None:
        node = _SingleDeliverNode()
        node.name = "single_deliver"
        ctx, dispatch_calls = _make_parallel_ctx()
        await node.run(ctx)
        assert len(dispatch_calls) == 1
        _source, target, payload = dispatch_calls[0]
        assert target == "downstream_a"
        assert payload and payload["delivered"] == "payload_a"

    async def test_submit_under_parallel_multiple_groups(self) -> None:
        node = _MultiDeliverNode()
        node.name = "multi_deliver"
        ctx, dispatch_calls = _make_parallel_ctx()
        await node.run(ctx)
        assert len(dispatch_calls) == 3
        targets = [call[1] for call in dispatch_calls]
        assert targets.count("target_x") == 2
        assert targets.count("target_y") == 1

    async def test_submit_under_parallel_multi_entry_group_dispatches_individually(self) -> None:
        node = _MultiDeliverNode()
        node.name = "multi_deliver"
        ctx, dispatch_calls = _make_parallel_ctx()
        await node.run(ctx)
        # target_x has 2 entries -> 2 separate dispatches, each with one content.
        target_x_payloads = [p for _s, t, p in dispatch_calls if t == "target_x"]
        target_y_payloads = [p for _s, t, p in dispatch_calls if t == "target_y"]
        assert len(target_x_payloads) == 2
        assert target_x_payloads[0]["delivered"] == "data_1"
        assert target_x_payloads[1]["delivered"] == "data_2"
        assert len(target_y_payloads) == 1
        assert target_y_payloads[0]["delivered"] == "data_3"

    async def test_custom_submit_override(self) -> None:
        node = _CustomSubmitNode()
        node.name = "custom_submit"
        ctx = _make_linear_ctx()
        await node.run(ctx)
        # Custom submit stores its own result, overriding the default _submit.
        assert node._submit_result == {"custom_submit": ["overridden"]}


# ── _resolve_default_target limitation ────────────────────────────────────


class _NullNextNodeDeliver(Node[CounterState]):
    """Node that delivers without specifying next_node."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver("data", None, ctx)  # next_node=None
        return None


class TestResolveDefaultTargetLimitation:
    async def test_deliver_without_next_node_raises_in_submit(self) -> None:
        node = _NullNextNodeDeliver()
        node.name = "null_next"
        ctx = _make_linear_ctx()
        with pytest.raises(RoutingError, match="graph topology"):
            await node.run(ctx)

    def test_resolve_default_target_raises_directly(self) -> None:
        node = _NullNextNodeDeliver()
        node.name = "null_next"
        ctx = _make_linear_ctx()
        with pytest.raises(RoutingError, match="graph topology"):
            node._resolve_default_target(ctx)

    async def test_resolve_default_target_with_graph(self) -> None:
        """Passing graph= to run() resolves None via default edges."""
        from helpers import AddNode

        from modex_graph import Graph, GraphNode, LinearScheduler

        g: Graph[CounterState] = Graph()
        g.add_node("null_next", _NullNextNodeDeliver())
        g.add_node("downstream", AddNode(amount=1))
        g.add_edge(GraphNode.START, "null_next")
        g.add_edge("null_next", "downstream")
        g.add_edge("downstream", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState())
        scheduler = LinearScheduler(compiled)
        result = await scheduler.run_async(ctx)
        assert result.count == 1

    async def test_resolve_default_target_no_default_uses_downstream(self) -> None:
        """No default edge → all downstream edges are targets."""
        from helpers import AddNode

        from modex_graph import Graph, GraphNode, LinearScheduler

        g: Graph[CounterState] = Graph()
        g.add_node("null_next", _NullNextNodeDeliver())
        g.add_node("target_a", AddNode(amount=1))
        g.add_edge(GraphNode.START, "null_next")
        g.add_edge("null_next", "target_a")
        g.add_edge("target_a", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState())
        scheduler = LinearScheduler(compiled)
        result = await scheduler.run_async(ctx)
        assert result.count == 1

    def test_resolve_default_target_strict_multi_edge_raises(self) -> None:
        """Strict policy (default) raises on multiple downstream edges."""
        from helpers import AddNode

        from modex_graph import Graph, GraphNode

        g: Graph[CounterState] = Graph()
        g.add_node("multi", _NullNextNodeDeliver())
        g.add_node("target_a", AddNode(amount=1))
        g.add_node("target_b", AddNode(amount=2))
        g.add_edge(GraphNode.START, "multi")
        g.add_edge("multi", "target_a")
        g.add_edge("multi", "target_b")
        compiled = g.compile()
        node = _NullNextNodeDeliver()
        node.name = "multi"
        node._graph_ref = compiled
        ctx = _make_linear_ctx()
        with pytest.raises(RoutingError, match="downstream targets"):
            node._resolve_default_target(ctx)

    def test_resolve_default_target_graceful_multi_edge_returns_end(self) -> None:
        """Graceful policy returns [END] on multiple downstream edges."""
        from helpers import AddNode

        from modex_graph import Graph, GraphNode

        g: Graph[CounterState] = Graph()
        g.add_node("multi", _NullNextNodeDeliver())
        g.add_node("target_a", AddNode(amount=1))
        g.add_node("target_b", AddNode(amount=2))
        g.add_edge(GraphNode.START, "multi")
        g.add_edge("multi", "target_a")
        g.add_edge("multi", "target_b")
        compiled = g.compile()
        node = _NullNextNodeDeliver()
        node.name = "multi"
        node._graph_ref = compiled
        ctx = _make_linear_ctx()
        assert node._resolve_default_target(ctx, policy="graceful") == [GraphNode.END]

    def test_resolve_default_target_graceful_single_edge_returns_target(self) -> None:
        """Graceful policy with one downstream edge returns that target."""
        from helpers import AddNode

        from modex_graph import Graph, GraphNode

        g: Graph[CounterState] = Graph()
        g.add_node("single", _NullNextNodeDeliver())
        g.add_node("only_down", AddNode(amount=1))
        g.add_edge(GraphNode.START, "single")
        g.add_edge("single", "only_down")
        compiled = g.compile()
        node = _NullNextNodeDeliver()
        node.name = "single"
        node._graph_ref = compiled
        ctx = _make_linear_ctx()
        assert node._resolve_default_target(ctx, policy="graceful") == ["only_down"]


# ── DeliverStore-backed persistence ───────────────────────────────────────


class TestDeliverStoreBacked:
    """Ticket 16: _deliver/_collect_delivers are now in-memory only.

    The deliver_store/graph_instance_id persistence branches were removed.
    These tests verify the new behavior: delivers always go to
    _pending_delivers, never to the store, and _deliver always returns None.
    """

    async def test_deliver_does_not_persist_to_store(self) -> None:
        store = InMemoryDeliverStore()
        node = _StoreBackedNode(store)
        ctx = _make_linear_ctx()
        ctx.graph_instance_id = 5001
        node._pending_delivers = []
        node._deliver({"key": "value"}, "db_target", ctx)
        node._deliver("second", "db_target", ctx)
        # Store should be empty — _deliver is in-memory only now.
        assert store.query_consumable(5001, "store_node") == []
        # Entries are in _pending_delivers instead.
        assert len(node._pending_delivers) == 2

    async def test_collect_delivers_reads_from_in_memory(self) -> None:
        store = InMemoryDeliverStore()
        node = _StoreBackedNode(store)
        ctx = _make_linear_ctx()
        ctx.graph_instance_id = 5002
        node._pending_delivers = []
        node._deliver({"key": "value"}, "db_target", ctx)
        node._deliver("second", "db_target", ctx)
        collected = node._collect_delivers(ctx)
        assert len(collected) == 2
        # next_node is preserved from the in-memory entry.
        assert all(next_node == "db_target" for _, next_node in collected)

    async def test_deliver_store_async_node_in_memory(self) -> None:
        store = InMemoryDeliverStore()
        node = _StoreBackedAsyncNode(store)
        ctx = _make_linear_ctx()
        ctx.graph_instance_id = 5003
        node._pending_delivers = []
        node._deliver("async_db_data", "async_db_target", ctx)
        # Store should be empty — in-memory only.
        assert store.query_consumable(5003, "async_store_node") == []
        assert len(node._pending_delivers) == 1

    async def test_deliver_without_graph_instance_id_uses_in_memory(self) -> None:
        """If ctx.graph_instance_id is None, falls back to in-memory accumulation."""
        store = InMemoryDeliverStore()
        node = _StoreBackedNode(store)
        ctx = _make_linear_ctx()
        await node.run(ctx)
        assert "db_target" in node._submit_result
        assert len(node._submit_result["db_target"]) == 2

    async def test_deliver_in_memory_under_parallel(self) -> None:
        store = InMemoryDeliverStore()
        node = _StoreBackedNode(store)
        ctx, _dispatch_calls = _make_parallel_ctx()
        ctx.graph_instance_id = 5004
        node._pending_delivers = []
        node._deliver({"key": "value"}, "db_target", ctx)
        node._deliver("second", "db_target", ctx)
        # Store should be empty — in-memory only.
        assert store.query_consumable(5004, "store_node") == []
        assert len(node._pending_delivers) == 2

    async def test_deliver_always_returns_none_even_with_store(self) -> None:
        store = InMemoryDeliverStore()
        node = _StoreBackedNode(store)
        ctx = _make_linear_ctx()
        ctx.graph_instance_id = 5005
        node._pending_delivers = []
        deliver_id = node._deliver("test_data", "test_target", ctx)
        # Ticket 16: _deliver always returns None (in-memory only).
        assert deliver_id is None

    async def test_deliver_returns_none_when_in_memory(self) -> None:
        node = _SingleDeliverNode()
        node.name = "single_deliver"
        ctx = _make_linear_ctx()
        node._pending_delivers = []
        deliver_id = node._deliver("test_data", "test_target", ctx)
        assert deliver_id is None


# ── _collect_delivers ─────────────────────────────────────────────────────


class TestCollectDelivers:
    async def test_collect_from_in_memory(self) -> None:
        node = _MultiDeliverNode()
        node.name = "multi_deliver"
        ctx = _make_linear_ctx()
        await node.run(ctx)
        # After _execute, _pending_delivers has the raw entries.
        # _collect_delivers reads from _pending_delivers (no store set).
        collected = node._collect_delivers(ctx)
        assert len(collected) == 3

    async def test_collect_from_store(self) -> None:
        # Ticket 16: _collect_delivers always reads from _pending_delivers
        # (in-memory only). The deliver_store is no longer consulted.
        store = InMemoryDeliverStore()
        node = _StoreBackedNode(store)
        ctx = _make_linear_ctx()
        ctx.graph_instance_id = 5006
        node._pending_delivers = []
        node._deliver({"key": "value"}, "db_target", ctx)
        node._deliver("second", "db_target", ctx)
        collected = node._collect_delivers(ctx)
        assert len(collected) == 2
        # Store is empty — _deliver is in-memory only.
        assert store.query_consumable(5006, "store_node") == []


# ── Integration: full _execute flow with DeliverStore ─────────────────────


class TestFullFlowWithStore:
    """Ticket 16: _deliver is in-memory only. These tests verify the
    full flow works correctly with deliver_store set but unused."""

    async def test_full_flow_sync_node(self) -> None:
        store = InMemoryDeliverStore()
        node = _StoreBackedNode(store)
        ctx = _make_linear_ctx(CounterState(count=0))
        ctx.graph_instance_id = 5007
        node._pending_delivers = []
        node._deliver({"key": "value"}, "db_target", ctx)
        node._deliver("second", "db_target", ctx)
        # Store is empty — in-memory only.
        assert store.query_consumable(5007, "store_node") == []
        assert len(node._pending_delivers) == 2

    async def test_full_flow_async_node(self) -> None:
        store = InMemoryDeliverStore()
        node = _StoreBackedAsyncNode(store)
        ctx = _make_linear_ctx(CounterState(count=0))
        ctx.graph_instance_id = 5008
        node._pending_delivers = []
        node._deliver("async_db_data", "async_db_target", ctx)
        # Store is empty — in-memory only.
        assert store.query_consumable(5008, "async_store_node") == []
        assert len(node._pending_delivers) == 1

    async def test_full_flow_with_upstream_payloads_and_deliver(self) -> None:
        """Node receives upstream payloads, integrates them, and delivers output."""

        class _TransformNode(Node[CounterState]):
            async def execute(
                self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
            ) -> None:
                if integrated_input.integrated_content is not None:
                    self.deliver(
                        integrated_input.integrated_content,
                        "transformed_output",
                        ctx,
                    )
                return None

        node = _TransformNode()
        node.name = "transform"
        node.node_id = "node-transform"
        ctx = _make_linear_ctx()
        ctx.coordinator.register_node(node.node_id)
        store = ctx.coordinator.get_deliver_store(node.node_id)
        assert store is not None
        store.accumulate(
            graph_instance_id=0,
            node_id=node.node_id,
            source_node_id="up_a",
            source_invocation_id=1,
            content="input1",
        )
        store.accumulate(
            graph_instance_id=0,
            node_id=node.node_id,
            source_node_id="up_b",
            source_invocation_id=2,
            content="input2",
        )
        await node.run(ctx)
        assert "transformed_output" in node._submit_result
        assert node._submit_result["transformed_output"] == [["input1", "input2"]]


# ── GraphNode.END as next_node ────────────────────────────────────────────


class _DeliverToEndNode(Node[CounterState]):
    """Node that delivers to GraphNode.END."""

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver("terminal_data", GraphNode.END, ctx)
        return None


class TestDeliverToEnd:
    async def test_deliver_to_end_sentinel(self) -> None:
        node = _DeliverToEndNode()
        node.name = "deliver_to_end"
        ctx = _make_linear_ctx()
        await node.run(ctx)
        assert GraphNode.END in node._submit_result
        assert node._submit_result[GraphNode.END] == ["terminal_data"]

    async def test_deliver_to_end_under_parallel(self) -> None:
        node = _DeliverToEndNode()
        node.name = "deliver_to_end"
        ctx, dispatch_calls = _make_parallel_ctx()
        await node.run(ctx)
        assert len(dispatch_calls) == 1
        _source, target, payload = dispatch_calls[0]
        assert target == GraphNode.END
        assert payload and payload["delivered"] == "terminal_data"


# ── Undelivered detection ─────────────────────────────────────


class _RetrySucceedsNode(Node[CounterState]):
    """Does not deliver on first execute; delivers on second (retry)."""

    def __init__(self) -> None:
        self.execute_count = 0

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.execute_count += 1
        if self.execute_count >= 2:
            self.deliver("recovered", "downstream", ctx)
        return None


class _NeverDeliverNode(Node[CounterState]):
    """Never calls deliver — triggers RoutingError after max_retry."""

    def __init__(self) -> None:
        self.execute_count = 0

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.execute_count += 1
        return None


class _CountingDeliverNode(Node[CounterState]):
    """Always delivers. Tracks execute count to verify no retry."""

    def __init__(self) -> None:
        self.execute_count = 0

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.execute_count += 1
        self.deliver("data", "target", ctx)
        return None


class _ErrorFeedbackInspectorNode(Node[CounterState]):
    """Records integrated input on each execute. Delivers on second."""

    def __init__(self) -> None:
        self.execute_count = 0
        self.seen_integrated_inputs: list[IntegratedInput] = []

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.execute_count += 1
        self.seen_integrated_inputs.append(integrated_input)
        if self.execute_count >= 2:
            self.deliver("done", "target", ctx)
        return None


class TestUndeliveredDetection:
    async def test_undelivered_detection_retries_with_error_feedback(self) -> None:
        node = _RetrySucceedsNode()
        node.name = "retry_succeeds"
        ctx = _make_linear_ctx()
        result = await node.run(ctx)
        assert result is None
        assert node.execute_count == 2
        assert "downstream" in node._submit_result

    async def test_undelivered_detection_raises_after_max_retry(self) -> None:
        node = _NeverDeliverNode()
        node.name = "never_deliver"
        ctx = _make_linear_ctx()
        with pytest.raises(RoutingError, match="produced no delivers"):
            await node.run(ctx)
        assert node.execute_count == 4

    async def test_undelivered_detection_custom_max_retry(self) -> None:
        node = _NeverDeliverNode()
        node.name = "never_deliver"
        node.max_retry = 1
        ctx = _make_linear_ctx()
        with pytest.raises(RoutingError, match="produced no delivers"):
            await node.run(ctx)
        assert node.execute_count == 2

    async def test_undelivered_detection_no_retry_when_delivers_present(self) -> None:
        node = _CountingDeliverNode()
        node.name = "always_deliver"
        ctx = _make_linear_ctx()
        await node.run(ctx)
        assert node.execute_count == 1
        assert "target" in node._submit_result

    async def test_undelivered_detection_error_feedback_in_integrated_input(self) -> None:
        node = _ErrorFeedbackInspectorNode()
        node.name = "inspector"
        ctx = _make_linear_ctx()
        await node.run(ctx)
        assert node.execute_count == 2
        first_input = node.seen_integrated_inputs[0]
        assert first_input is not None
        assert first_input.payloads == []
        second_input = node.seen_integrated_inputs[1]
        assert second_input is not None
        assert len(second_input.payloads) == 1
        feedback = second_input.payloads[0]
        assert feedback.source_node == "__framework__"
        assert isinstance(feedback.content, dict)
        assert feedback.content["error"] == "undelivered"
        assert feedback.metadata["error_type"] == "undelivered"
        assert feedback.metadata["retry"] == 1

    async def test_undelivered_detection_max_retry_zero(self) -> None:
        node = _NeverDeliverNode()
        node.name = "never_deliver"
        node.max_retry = 0
        ctx = _make_linear_ctx()
        with pytest.raises(RoutingError, match="produced no delivers"):
            await node.run(ctx)
        assert node.execute_count == 1


# ── Rule 15 convergence: _submit calls ctx.dispatch for BOTH schedulers ───


class TestSubmitDispatchConvergence:
    """Rule 15: _submit has NO scheduler_kind branch — it always calls
    ctx.dispatch. Both LINEAR and PARALLEL schedulers register a handler.
    """

    async def test_submit_calls_dispatch_under_linear(self) -> None:
        """_submit calls ctx.dispatch under LINEAR (no branch on scheduler_kind)."""
        node = _SingleDeliverNode()
        node.name = "single_deliver"
        calls: list[tuple[str, str, dict[str, Any] | None]] = []

        ctx: GraphContext[CounterState] = GraphContext(
            state=CounterState(),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.LINEAR,
        )

        def handler(src: str, tgt: str, update: dict[str, Any] | None) -> None:
            calls.append((src, tgt, update))

        ctx.set_dispatch_handler(handler)
        await node.run(ctx)
        assert len(calls) == 1
        _src, target, payload = calls[0]
        assert target == "downstream_a"
        assert payload and payload["delivered"] == "payload_a"

    async def test_submit_calls_dispatch_under_parallel(self) -> None:
        """_submit calls ctx.dispatch under PARALLEL (same path as LINEAR)."""
        node = _SingleDeliverNode()
        node.name = "single_deliver"
        ctx, dispatch_calls = _make_parallel_ctx()
        await node.run(ctx)
        assert len(dispatch_calls) == 1
        _src, target, payload = dispatch_calls[0]
        assert target == "downstream_a"
        assert payload and payload["delivered"] == "payload_a"

    async def test_submit_dispatches_all_groups_under_both(self) -> None:
        """Multiple groups are all dispatched under both scheduler kinds."""

        def make_ctx_with_handler(
            kind: SchedulerKind,
        ) -> tuple[
            GraphContext[CounterState],
            list[tuple[str, str, dict[str, Any] | None]],
        ]:
            calls: list[tuple[str, str, dict[str, Any] | None]] = []
            ctx = GraphContext(
                state=CounterState(),
                runtime=make_runtime(),
                coordinator=make_coordinator(),
                scheduler_kind=kind,
            )
            ctx.set_dispatch_handler(lambda s, t, p: calls.append((s, t, p)))
            return ctx, calls

        for kind in (SchedulerKind.LINEAR, SchedulerKind.PARALLEL):
            node = _MultiDeliverNode()
            node.name = "multi_deliver"
            ctx, calls = make_ctx_with_handler(kind)
            await node.run(ctx)
            assert len(calls) == 3, f"Expected 3 dispatches under {kind}, got {len(calls)}"
            targets = [c[1] for c in calls]
            assert targets.count("target_x") == 2
            assert targets.count("target_y") == 1


# ── upstream_payloads flow: deliver → integrated_input ────────────────────


class _RecordingSinkNode(Node[CounterState]):
    """Records integrated_input on each execute, then delivers to END."""

    def __init__(self) -> None:
        self.seen_inputs: list[IntegratedInput] = []

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.seen_inputs.append(integrated_input)
        self.deliver("sink_done", GraphNode.END, ctx)
        return None


class _SourceNode(Node[CounterState]):
    """Delivers a fixed content to a target."""

    def __init__(self, content: Any, target: str) -> None:
        self.content = content
        self.target = target

    async def execute(
        self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver(self.content, self.target, ctx)
        return None


class TestUpstreamPayloadsFlow:
    """Delivers flow from Node A's deliver → Node B's
    integrated_input under both LINEAR and PARALLEL schedulers.

    The dispatch handler calls ``coordinator.route_deliver`` to route
    delivers to the target node's deliver_store, and ``Node.run()``
    consumes them via ``collect_consumable_delivers``.
    """

    async def test_flow_under_linear(self) -> None:
        """Node A delivers content → Node B receives it via integrated_input."""
        from modex_graph import Graph, LinearScheduler

        sink = _RecordingSinkNode()
        g: Graph[CounterState] = Graph()
        g.add_node("a", _SourceNode(content="from_a", target="b"))
        g.add_node("b", sink)
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile()

        ctx = GraphContext(
            state=CounterState(count=0),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
        )
        await LinearScheduler(compiled).run_async(ctx)

        assert len(sink.seen_inputs) == 1
        integrated = sink.seen_inputs[0]
        assert len(integrated.payloads) == 1
        assert integrated.payloads[0].source_node == compiled.nodes["a"].node_id
        assert integrated.payloads[0].content == "from_a"
        assert integrated.integrated_content == ["from_a"]

    async def test_flow_under_linear_multiple_delivers(self) -> None:
        """Node A delivers multiple contents to Node B → _submit groups them
        into one dispatch with a list payload. Node B receives ONE
        IntegratedPayload whose content is the list."""
        from modex_graph import Graph, LinearScheduler

        class MultiSourceNode(Node[CounterState]):
            async def execute(
                self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
            ) -> None:
                self.deliver("first", "b", ctx)
                self.deliver("second", "b", ctx)
                return None

        sink = _RecordingSinkNode()
        g: Graph[CounterState] = Graph()
        g.add_node("a", MultiSourceNode())
        g.add_node("b", sink)
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile()

        ctx = make_ctx(CounterState(count=0))
        await LinearScheduler(compiled).run_async(ctx)

        assert len(sink.seen_inputs) == 1
        integrated = sink.seen_inputs[0]
        assert len(integrated.payloads) == 2
        assert integrated.payloads[0].source_node == compiled.nodes["a"].node_id
        assert integrated.payloads[0].content == "first"
        assert integrated.payloads[1].content == "second"
        assert integrated.integrated_content == ["first", "second"]

    async def test_flow_under_parallel_on_receive(self) -> None:
        """Under PARALLEL + ON_RECEIVE: Node A delivers → Node B receives
        the dispatch payload as an IntegratedPayload via upstream_payloads."""
        from modex_graph import (
            Graph,
            NodeTrigger,
            ParallelScheduler,
        )

        sink = _RecordingSinkNode()
        g: Graph[CounterState] = Graph()
        g.add_node("a", _SourceNode(content={"data": 42}, target="b"))
        g.add_node("b", sink)
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", GraphNode.END)
        compiled = g.compile(
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_RECEIVE,
        )

        ctx = GraphContext(
            state=CounterState(count=0),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        await ParallelScheduler(compiled).run_async(ctx)

        assert len(sink.seen_inputs) == 1
        integrated = sink.seen_inputs[0]
        assert len(integrated.payloads) == 1
        assert integrated.payloads[0].source_node == compiled.nodes["a"].node_id
        assert integrated.payloads[0].content == {"data": 42}

    async def test_flow_under_parallel_on_all_preds(self) -> None:
        """Under PARALLEL + ON_ALL_PREDS: two DIFFERENT source nodes deliver
        to the same target → the target receives one IntegratedPayload per
        source via upstream_payloads."""
        from modex_graph import (
            Graph,
            NodeTrigger,
            ParallelScheduler,
        )

        class FanOutNode(Node[CounterState]):
            async def execute(
                self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
            ) -> None:
                self.deliver("from_a", "b", ctx)
                self.deliver("from_a", "c", ctx)
                return None

        sink = _RecordingSinkNode()
        g: Graph[CounterState] = Graph()
        g.add_node("a", FanOutNode())
        g.add_node("b", _SourceNode(content="from_b", target="d"))
        g.add_node("c", _SourceNode(content="from_c", target="d"))
        g.add_node("d", sink)
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", "d")
        g.add_edge("c", "d")
        g.add_edge("d", GraphNode.END)
        compiled = g.compile(
            scheduler=SchedulerKind.PARALLEL,
            default_trigger=NodeTrigger.ON_ALL_PREDS,
        )

        ctx = GraphContext(
            state=CounterState(count=0),
            runtime=make_runtime(),
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        await ParallelScheduler(compiled).run_async(ctx)

        assert len(sink.seen_inputs) == 1
        integrated = sink.seen_inputs[0]
        # Two different sources (b, c) → two IntegratedPayloads.
        assert len(integrated.payloads) == 2
        by_source = {p.source_node: p.content for p in integrated.payloads}
        assert by_source == {
            compiled.nodes["b"].node_id: "from_b",
            compiled.nodes["c"].node_id: "from_c",
        }

    async def test_entry_node_receives_start_payload(self) -> None:
        from modex_graph import Graph, LinearScheduler

        sink = _RecordingSinkNode()
        g: Graph[CounterState] = Graph()
        g.add_node("entry", sink)
        g.add_edge(GraphNode.START, "entry")
        g.add_edge("entry", GraphNode.END)
        compiled = g.compile()

        ctx = make_ctx(CounterState(count=0))
        await LinearScheduler(compiled).run_async(ctx)

        assert len(sink.seen_inputs) == 1
        integrated = sink.seen_inputs[0]
        assert len(integrated.payloads) == 1
        assert integrated.payloads[0].source_node == compiled.nodes[GraphNode.START].node_id
        assert integrated.integrated_content == [None]
