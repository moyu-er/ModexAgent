# ruff: noqa: ANN401
"""Tests for Node._execute / _deliver / deliver / _submit / submit (ticket 07).

Covers the additive deliver/submit dual-method API on `Node`:

- `_execute`: orchestrate integrate -> execute -> _submit, return NodeResult.
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
from helpers import CounterState, make_ctx, make_runtime

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
    NodeResult,
    RoutingError,
    SchedulerKind,
)

# ── Test node subclasses ──────────────────────────────────────────────────


class _NoDeliverNode(Node[CounterState]):
    """Node that does not call deliver. _submit_result should be empty."""

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        ctx.state.count += 1
        return NodeResult()


class _SingleDeliverNode(Node[CounterState]):
    """Node that delivers once to an explicit next_node."""

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        self.deliver("payload_a", "downstream_a", ctx)
        return NodeResult()


class _MultiDeliverNode(Node[CounterState]):
    """Node that delivers multiple times to different next_nodes."""

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        self.deliver("data_1", "target_x", ctx)
        self.deliver("data_2", "target_x", ctx)
        self.deliver("data_3", "target_y", ctx)
        return NodeResult()


class _AsyncDeliverNode(Node[CounterState]):
    """Async node that delivers during async execute."""

    async def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        self.deliver("async_data", "async_target", ctx)
        return NodeResult()


class _DeliverWithCtxNode(Node[CounterState]):
    """Node that passes ctx explicitly to deliver()."""

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        self.deliver("explicit_ctx_data", "explicit_target", ctx)
        return NodeResult()


class _ReadIntegratedInputNode(Node[CounterState]):
    """Node that reads integrated_input during execute."""

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        if integrated_input.integrated_content is not None:
            ctx.state.name = str(integrated_input.integrated_content)
        self.deliver("read", "read_target", ctx)
        return NodeResult()


class _CustomDeliverNode(Node[CounterState]):
    """Node that overrides deliver() to add a prefix."""

    def deliver(
        self,
        content: Any,
        next_node: str | None,
        ctx: GraphContext[CounterState],
    ) -> None:
        super().deliver(f"custom:{content}", next_node, ctx)

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        self.deliver("data", "target", ctx)
        return NodeResult()


class _CustomSubmitNode(Node[CounterState]):
    """Node that overrides submit() to store a custom result."""

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        self.deliver("data", "target", ctx)
        return NodeResult()

    def submit(self, ctx: GraphContext[CounterState]) -> None:
        self._submit_result = {"custom_submit": ["overridden"]}


class _StoreBackedNode(Node[CounterState]):
    """Node configured with a DeliverStore. Used for persistence tests."""

    def __init__(self, store: DeliverStore) -> None:
        self.name = "store_node"
        self.deliver_store = store

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        self.deliver({"key": "value"}, "db_target", ctx)
        self.deliver("second", "db_target", ctx)
        return NodeResult()


class _StoreBackedAsyncNode(Node[CounterState]):
    """Async node configured with a DeliverStore."""

    def __init__(self, store: DeliverStore) -> None:
        self.name = "async_store_node"
        self.deliver_store = store

    async def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        self.deliver("async_db_data", "async_db_target", ctx)
        return NodeResult()


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

    def test_deliver_store_defaults_to_none(self) -> None:
        node = _NoDeliverNode()
        assert node.deliver_store is None

    def test_input_integrator_can_be_overridden(self) -> None:
        node = _NoDeliverNode()
        custom = _JoinIntegrator()
        node.input_integrator = custom
        assert node.input_integrator is custom

    def test_deliver_store_can_be_set(self) -> None:
        node = _NoDeliverNode()
        store = InMemoryDeliverStore()
        node.deliver_store = store
        assert node.deliver_store is store


# ── _execute: basic orchestration ─────────────────────────────────────────


class TestExecuteBasic:
    async def test_execute_calls_execute_and_returns_result(self) -> None:
        node = _SingleDeliverNode()
        node.name = "single_deliver"
        ctx = _make_linear_ctx(CounterState(count=0))
        result = await node.run(ctx)
        assert isinstance(result, NodeResult)
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
        ctx = _make_linear_ctx(CounterState(name=""))
        payloads = [
            IntegratedPayload(source_node="up_a", content="hello"),
            IntegratedPayload(source_node="up_b", content="world"),
        ]
        await node.run(ctx, upstream_payloads=payloads)
        assert ctx.state.name == "['hello', 'world']"

    async def test_execute_with_no_upstream_payloads(self) -> None:
        node = _ReadIntegratedInputNode()
        node.name = "read_integrated"
        ctx = _make_linear_ctx(CounterState(name="initial"))
        await node.run(ctx)
        # Default integrator on empty list -> integrated_content = []
        assert ctx.state.name == "[]"

    async def test_execute_with_custom_integrator(self) -> None:
        node = _ReadIntegratedInputNode()
        node.name = "read_integrated"
        node.input_integrator = _JoinIntegrator()
        ctx = _make_linear_ctx(CounterState(name=""))
        payloads = [
            IntegratedPayload(source_node="up_a", content="hello"),
            IntegratedPayload(source_node="up_b", content="world"),
        ]
        await node.run(ctx, upstream_payloads=payloads)
        assert ctx.state.name == "hello + world"

    async def test_execute_with_async_node(self) -> None:
        node = _AsyncDeliverNode()
        node.name = "async_deliver"
        ctx = _make_linear_ctx()
        result = await node.run(ctx)
        assert isinstance(result, NodeResult)
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
        assert payload == {"delivered": "payload_a"}

    async def test_submit_under_parallel_multiple_groups(self) -> None:
        node = _MultiDeliverNode()
        node.name = "multi_deliver"
        ctx, dispatch_calls = _make_parallel_ctx()
        await node.run(ctx)
        assert len(dispatch_calls) == 2
        targets = {call[1] for call in dispatch_calls}
        assert targets == {"target_x", "target_y"}

    async def test_submit_under_parallel_multi_entry_group_dispatches_list(self) -> None:
        node = _MultiDeliverNode()
        node.name = "multi_deliver"
        ctx, dispatch_calls = _make_parallel_ctx()
        await node.run(ctx)
        # target_x has 2 entries -> dispatched as a list.
        for _source, target, payload in dispatch_calls:
            if target == "target_x":
                assert payload == {"delivered": ["data_1", "data_2"]}
            elif target == "target_y":
                assert payload == {"delivered": "data_3"}

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

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        self.deliver("data", None, ctx)  # next_node=None
        return NodeResult()


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


# ── DeliverStore-backed persistence ───────────────────────────────────────


class TestDeliverStoreBacked:
    async def test_deliver_persists_to_store(self) -> None:
        store = InMemoryDeliverStore()
        node = _StoreBackedNode(store)
        ctx = _make_linear_ctx()
        ctx.graph_instance_id = 5001
        await node.run(ctx)
        # After _submit, records are marked SUBMITTED — query_pending
        # (ACCUMULATED only) returns empty. Verify dispatch via
        # _submit_result and confirm mark_submitted took effect.
        assert "db_target" in node._submit_result
        assert len(node._submit_result["db_target"]) == 2
        contents = node._submit_result["db_target"]
        assert {"key": "value"} in contents
        assert "second" in contents
        assert store.query_pending(5001, "store_node") == []

    async def test_submit_groups_from_store(self) -> None:
        store = InMemoryDeliverStore()
        node = _StoreBackedNode(store)
        ctx = _make_linear_ctx()
        ctx.graph_instance_id = 5002
        await node.run(ctx)
        assert "db_target" in node._submit_result
        assert len(node._submit_result["db_target"]) == 2

    async def test_deliver_store_async_node(self) -> None:
        store = InMemoryDeliverStore()
        node = _StoreBackedAsyncNode(store)
        ctx = _make_linear_ctx()
        ctx.graph_instance_id = 5003
        result = await node.run(ctx)
        assert isinstance(result, NodeResult)
        assert "async_db_target" in node._submit_result
        assert node._submit_result["async_db_target"] == ["async_db_data"]
        assert store.query_pending(5003, "async_store_node") == []

    async def test_deliver_without_graph_instance_id_uses_in_memory(self) -> None:
        """If ctx.graph_instance_id is None, falls back to in-memory accumulation."""
        store = InMemoryDeliverStore()
        node = _StoreBackedNode(store)
        ctx = _make_linear_ctx()
        # Note: NOT setting ctx.graph_instance_id (defaults to None).
        await node.run(ctx)
        # Store should be empty (no graph_instance_id to key on).
        # _submit_result should still have the data (from in-memory path).
        assert "db_target" in node._submit_result
        assert len(node._submit_result["db_target"]) == 2

    async def test_deliver_store_under_parallel(self) -> None:
        store = InMemoryDeliverStore()
        node = _StoreBackedNode(store)
        ctx, dispatch_calls = _make_parallel_ctx()
        ctx.graph_instance_id = 5004
        await node.run(ctx)
        assert len(dispatch_calls) == 1
        _source, target, payload = dispatch_calls[0]
        assert target == "db_target"
        # 2 entries -> dispatched as a list.
        assert payload == {"delivered": [{"key": "value"}, "second"]}

    async def test_deliver_returns_deliver_id_when_persisted(self) -> None:
        store = InMemoryDeliverStore()
        node = _StoreBackedNode(store)
        ctx = _make_linear_ctx()
        ctx.graph_instance_id = 5005
        # Call _deliver directly to check the return value.
        node._pending_delivers = []
        deliver_id = node._deliver("test_data", "test_target", ctx)
        assert deliver_id is not None
        assert isinstance(deliver_id, int)
        assert deliver_id > 0

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
        store = InMemoryDeliverStore()
        node = _StoreBackedNode(store)
        ctx = _make_linear_ctx()
        ctx.graph_instance_id = 5006
        await node.run(ctx)
        # After _submit, records are SUBMITTED → _collect_delivers
        # (which reads query_pending) returns empty. The dispatched
        # content is in _submit_result.
        collected = node._collect_delivers(ctx)
        assert len(collected) == 0
        assert "db_target" in node._submit_result


# ── Integration: full _execute flow with DeliverStore ─────────────────────


class TestFullFlowWithStore:
    async def test_full_flow_sync_node(self) -> None:
        store = InMemoryDeliverStore()
        node = _StoreBackedNode(store)
        ctx = _make_linear_ctx(CounterState(count=0))
        ctx.graph_instance_id = 5007
        result = await node.run(ctx)
        assert isinstance(result, NodeResult)
        assert "db_target" in node._submit_result
        assert len(node._submit_result["db_target"]) == 2
        assert store.query_pending(5007, "store_node") == []

    async def test_full_flow_async_node(self) -> None:
        store = InMemoryDeliverStore()
        node = _StoreBackedAsyncNode(store)
        ctx = _make_linear_ctx(CounterState(count=0))
        ctx.graph_instance_id = 5008
        result = await node.run(ctx)
        assert isinstance(result, NodeResult)
        assert "async_db_target" in node._submit_result
        assert store.query_pending(5008, "async_store_node") == []

    async def test_full_flow_with_upstream_payloads_and_deliver(self) -> None:
        """Node receives upstream payloads, integrates them, and delivers output."""

        class _TransformNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                if integrated_input.integrated_content is not None:
                    self.deliver(
                        integrated_input.integrated_content,
                        "transformed_output",
                        ctx,
                    )
                return NodeResult()

        node = _TransformNode()
        node.name = "transform"
        ctx = _make_linear_ctx()
        payloads = [
            IntegratedPayload(source_node="up_a", content="input1"),
            IntegratedPayload(source_node="up_b", content="input2"),
        ]
        await node.run(ctx, upstream_payloads=payloads)
        assert "transformed_output" in node._submit_result
        assert node._submit_result["transformed_output"] == [["input1", "input2"]]


# ── GraphNode.END as next_node ────────────────────────────────────────────


class _DeliverToEndNode(Node[CounterState]):
    """Node that delivers to GraphNode.END."""

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        self.deliver("terminal_data", GraphNode.END, ctx)
        return NodeResult()


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
        assert payload == {"delivered": "terminal_data"}


# ── Undelivered detection (ticket 03) ─────────────────────────────────────


class _RetrySucceedsNode(Node[CounterState]):
    """Does not deliver on first execute; delivers on second (retry)."""

    def __init__(self) -> None:
        self.execute_count = 0

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        self.execute_count += 1
        if self.execute_count >= 2:
            self.deliver("recovered", "downstream", ctx)
        return NodeResult()


class _NeverDeliverNode(Node[CounterState]):
    """Never calls deliver — triggers RoutingError after max_retry."""

    def __init__(self) -> None:
        self.execute_count = 0

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        self.execute_count += 1
        return NodeResult()


class _CountingDeliverNode(Node[CounterState]):
    """Always delivers. Tracks execute count to verify no retry."""

    def __init__(self) -> None:
        self.execute_count = 0

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        self.execute_count += 1
        self.deliver("data", "target", ctx)
        return NodeResult()


class _ErrorFeedbackInspectorNode(Node[CounterState]):
    """Records integrated input on each execute. Delivers on second."""

    def __init__(self) -> None:
        self.execute_count = 0
        self.seen_integrated_inputs: list[IntegratedInput] = []

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        self.execute_count += 1
        self.seen_integrated_inputs.append(integrated_input)
        if self.execute_count >= 2:
            self.deliver("done", "target", ctx)
        return NodeResult()


class TestUndeliveredDetection:
    async def test_undelivered_detection_retries_with_error_feedback(self) -> None:
        node = _RetrySucceedsNode()
        node.name = "retry_succeeds"
        ctx = _make_linear_ctx()
        result = await node.run(ctx)
        assert isinstance(result, NodeResult)
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
            scheduler_kind=SchedulerKind.LINEAR,
        )

        def handler(src: str, tgt: str, update: dict[str, Any] | None) -> None:
            calls.append((src, tgt, update))

        ctx.set_dispatch_handler(handler)
        await node.run(ctx)
        assert len(calls) == 1
        _src, target, payload = calls[0]
        assert target == "downstream_a"
        assert payload == {"delivered": "payload_a"}

    async def test_submit_calls_dispatch_under_parallel(self) -> None:
        """_submit calls ctx.dispatch under PARALLEL (same path as LINEAR)."""
        node = _SingleDeliverNode()
        node.name = "single_deliver"
        ctx, dispatch_calls = _make_parallel_ctx()
        await node.run(ctx)
        assert len(dispatch_calls) == 1
        _src, target, payload = dispatch_calls[0]
        assert target == "downstream_a"
        assert payload == {"delivered": "payload_a"}

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
                scheduler_kind=kind,
            )
            ctx.set_dispatch_handler(lambda s, t, p: calls.append((s, t, p)))
            return ctx, calls

        for kind in (SchedulerKind.LINEAR, SchedulerKind.PARALLEL):
            node = _MultiDeliverNode()
            node.name = "multi_deliver"
            ctx, calls = make_ctx_with_handler(kind)
            await node.run(ctx)
            assert len(calls) == 2, f"Expected 2 dispatches under {kind}, got {len(calls)}"
            targets = {c[1] for c in calls}
            assert targets == {"target_x", "target_y"}


# ── upstream_payloads flow: deliver → integrated_input ────────────────────


class _RecordingSinkNode(Node[CounterState]):
    """Records integrated_input on each execute, then delivers to END."""

    def __init__(self) -> None:
        self.seen_inputs: list[IntegratedInput] = []

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        self.seen_inputs.append(integrated_input)
        self.deliver("sink_done", GraphNode.END, ctx)
        return NodeResult()


class _SourceNode(Node[CounterState]):
    """Delivers a fixed content to a target."""

    def __init__(self, content: Any, target: str) -> None:
        self.content = content
        self.target = target

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        self.deliver(self.content, self.target, ctx)
        return NodeResult()


class TestUpstreamPayloadsFlow:
    """upstream_payloads flows from Node A's deliver → Node B's
    integrated_input under both LINEAR and PARALLEL schedulers.
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
        )
        await LinearScheduler(compiled).run_async(ctx)

        assert len(sink.seen_inputs) == 1
        integrated = sink.seen_inputs[0]
        assert len(integrated.payloads) == 1
        assert integrated.payloads[0].source_node == "a"
        assert integrated.payloads[0].content == "from_a"
        assert integrated.integrated_content == ["from_a"]

    async def test_flow_under_linear_multiple_delivers(self) -> None:
        """Node A delivers multiple contents to Node B → _submit groups them
        into one dispatch with a list payload. Node B receives ONE
        IntegratedPayload whose content is the list."""
        from modex_graph import Graph, LinearScheduler

        class MultiSourceNode(Node[CounterState]):
            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                self.deliver("first", "b", ctx)
                self.deliver("second", "b", ctx)
                return NodeResult()

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
        # _submit groups multiple delivers to the same target into one
        # dispatch with a list payload → one IntegratedPayload.
        assert len(integrated.payloads) == 1
        assert integrated.payloads[0].source_node == "a"
        assert integrated.payloads[0].content == ["first", "second"]
        assert integrated.integrated_content == [["first", "second"]]

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
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        await ParallelScheduler(compiled).run_async(ctx)

        assert len(sink.seen_inputs) == 1
        integrated = sink.seen_inputs[0]
        assert len(integrated.payloads) == 1
        assert integrated.payloads[0].source_node == "a"
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
            def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
                self.deliver("from_a", "b", ctx)
                self.deliver("from_a", "c", ctx)
                return NodeResult()

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
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        await ParallelScheduler(compiled).run_async(ctx)

        assert len(sink.seen_inputs) == 1
        integrated = sink.seen_inputs[0]
        # Two different sources (b, c) → two IntegratedPayloads.
        assert len(integrated.payloads) == 2
        by_source = {p.source_node: p.content for p in integrated.payloads}
        assert by_source == {"b": "from_b", "c": "from_c"}

    async def test_entry_node_receives_empty_integrated_input(self) -> None:
        """The entry node has no upstream — integrated_input is empty (not None)."""
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
        assert integrated.payloads == []
        assert integrated.integrated_content == []
