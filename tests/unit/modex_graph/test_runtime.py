"""GraphRuntime no-op default tests."""

from __future__ import annotations

from helpers import CounterState, make_ctx

from modex_graph import GraphContext, GraphRuntime, Node
from modex_graph.integration import IntegratedInput
from modex_graph.scheduler.bootstrap import BootstrapMode


class TestGraphRuntimeNoOp:
    """GraphRuntime's default implementations are all no-ops."""

    async def test_before_node_no_op(self) -> None:
        runtime = GraphRuntime()
        ctx = make_ctx(CounterState())
        # Should not raise, should not mutate state.
        await runtime.before_node(ctx, "test_node")
        assert ctx.state.count == 0

    async def test_after_node_no_op(self) -> None:
        runtime = GraphRuntime()
        ctx = make_ctx(CounterState())
        await runtime.after_node(ctx, "test_node")
        assert ctx.state.count == 0

    async def test_dispatch_hook_no_op(self) -> None:
        runtime = GraphRuntime()
        ctx = make_ctx(CounterState())
        await runtime.dispatch_hook("some_hook", ctx, {"key": "value"})
        # No-op: no state change, no exception.

    async def test_dispatch_hook_none_data(self) -> None:
        runtime = GraphRuntime()
        ctx = make_ctx(CounterState())
        await runtime.dispatch_hook("some_hook", ctx, None)

    async def test_around_default_awaits_body(self) -> None:
        runtime = GraphRuntime()
        ctx = make_ctx(CounterState())
        called = False

        async def body() -> str:
            nonlocal called
            called = True
            return "result"

        result = await runtime.around("scope", ctx, body)
        assert called
        assert result == "result"

    async def test_apply_governance_returns_messages_unchanged(self) -> None:
        runtime = GraphRuntime()
        ctx = make_ctx(CounterState())
        messages = ["msg1", "msg2"]
        result = await runtime.apply_governance(messages, ctx)
        assert result is messages

    async def test_drain_control_no_op(self) -> None:
        runtime = GraphRuntime()
        ctx = make_ctx(CounterState())
        await runtime.drain_control(ctx)

    async def test_capture_snapshot_no_op(self) -> None:
        runtime = GraphRuntime()
        ctx = make_ctx(CounterState())
        await runtime.capture_snapshot(ctx, "test_reason")

    async def test_emit_no_op(self) -> None:
        runtime = GraphRuntime()
        ctx = make_ctx(CounterState())
        await runtime.emit("event_type", {"data": 1}, ctx)

    def test_runtime_is_abc(self) -> None:
        from abc import ABC

        assert issubclass(GraphRuntime, ABC)

    def test_runtime_no_before_iteration(self) -> None:
        """GraphRuntime does NOT have before_iteration/after_iteration (ADR-0033 D5)."""
        assert not hasattr(GraphRuntime, "before_iteration"), (
            "GraphRuntime must NOT have before_iteration — "
            "iteration is not a universal graph concept (ADR-0033 D5)."
        )
        assert not hasattr(GraphRuntime, "after_iteration"), (
            "GraphRuntime must NOT have after_iteration — "
            "iteration is not a universal graph concept (ADR-0033 D5)."
        )

    def test_runtime_has_8_methods(self) -> None:
        """2 engine-auto (before_node/after_node) + 6 node-explicit = 8 total."""
        expected = {
            "before_node",
            "after_node",
            "dispatch_hook",
            "around",
            "apply_governance",
            "drain_control",
            "capture_snapshot",
            "emit",
        }
        actual = {
            name
            for name in dir(GraphRuntime)
            if not name.startswith("_") and callable(getattr(GraphRuntime, name, None))
        }
        # Filter out ABC-inherited methods.
        actual &= expected
        assert actual == expected, f"Missing or extra methods: {expected ^ actual}"

    async def test_all_methods_are_async(self) -> None:
        """All GraphRuntime methods are async-only (ADR-0033 D5)."""
        import inspect

        runtime = GraphRuntime()
        for name in [
            "before_node",
            "after_node",
            "dispatch_hook",
            "around",
            "apply_governance",
            "drain_control",
            "capture_snapshot",
            "emit",
        ]:
            method = getattr(runtime, name)
            assert inspect.iscoroutinefunction(method), (
                f"GraphRuntime.{name} must be async (ADR-0033 D5)."
            )

    async def test_runtime_works_with_engine(self) -> None:
        """A no-op runtime works end-to-end with the engine."""
        from modex_graph import Graph, GraphEngine, GraphNode

        class AddNode(Node[CounterState]):
            def __init__(self, amount: int) -> None:
                self.amount = amount

            async def execute(
                self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput
            ) -> None:
                ctx.state.count += self.amount
                self.deliver(None, None, ctx)
                return None

        g: Graph[CounterState] = Graph()
        g.add_node("a", AddNode(5))
        g.add_edge(GraphNode.START, "a")
        g.add_edge("a", GraphNode.END)
        compiled = g.compile()
        ctx = make_ctx(CounterState())
        result = await GraphEngine(compiled).run_async(ctx, mode=BootstrapMode.FRESH)
        assert result.count == 5
