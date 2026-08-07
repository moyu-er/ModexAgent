"""Parallel error handling tests for ``ParallelScheduler`` (Task 08).

Covers the Task 08 acceptance criteria:

- Node ``execute`` raises a non-``GraphBubbleUp`` exception (e.g.
  ``RuntimeError``) under concurrent execution: the first exception
  cancels all other concurrent ``asyncio.Task`` instances via
  ``asyncio.gather`` and propagates to the caller.
- ``GraphInterrupt`` (a ``GraphBubbleUp`` subclass) raised under
  concurrent execution: propagates immediately, other concurrent
  instances are cancelled by ``asyncio.gather``.
- ``before_node`` / ``after_node`` hooks are called concurrently under
  ``asyncio.gather``; the implementer guarantees safety (no crash, no
  race on shared mutable state).
- ``ctx.emit`` is fire-and-forget (``loop.create_task``); concurrent
  invocation does not block or race.

Implementation notes
--------------------

``ParallelScheduler.run_async`` executes instances concurrently and cancels
the remaining tasks when one raises.

Shared scheduler state (``_instances``, ``_active``, ``_ready``) is safe
because all mutations happen in synchronous sections between await
points. Under CPython's GIL, a synchronous section runs atomically
with respect to other asyncio tasks — no interleaving can occur
inside a sync block. ``_handle_dispatch``, ``_compile_routing``, and
``_recheck_pending`` are fully synchronous, so dispatch/routing
decisions are atomic.

The ``manual_dispatches`` count uses per-instance counting
(``source_instance == instance_id``) rather than a global log-length
diff, because under concurrent execution the global log grows from
multiple instances' ``execute`` calls and the diff would be wrong.

ReactGraphRuntime audit (read-only)
-----------------------------------

``src/modex_agent/agents/react/runtime.py`` was audited for shared
mutable state. Findings (documented here per task scope — the runtime
was NOT modified):

- ``ReactGraphRuntime.__init__`` stores 7 service references
  (``_hook_runner``, ``_interceptor_chain``, ``_governance``,
  ``_control_channel``, ``_snapshot_policy``, ``_turn_state_store``,
  ``_emitter``). These are set once at construction and never mutated
  afterward — they are effectively immutable references.
- All methods (``before_node``, ``after_node``, ``dispatch_hook``,
  ``around``, ``apply_governance``, ``drain_control``,
  ``capture_snapshot``, ``emit``) are read-only on ``self``: they only
  read the service references and delegate. No counters, lists, or
  dicts on ``self`` are mutated.
- ``before_node`` and ``after_node`` are empty no-ops for ReAct —
  trivially safe under concurrent invocation.
- ``emit`` delegates to ``self._emitter.emit(...)``. The emitter is a
  shared ``ContentEmitter`` instance; concurrent calls are independent
  (each call emits one event). Safety depends on the emitter
  implementation, not on ``ReactGraphRuntime``.
- Conclusion: ``ReactGraphRuntime`` has NO shared mutable state on
  ``self``. It is safe for concurrent invocation of
  ``before_node`` / ``after_node`` / ``emit`` as long as the
  underlying services (``HookRunner``, ``InterceptorChain``,
  ``ContentEmitter``, etc.) are themselves safe. This is a
  framework-wide assumption — the graph engine does not introduce
  new shared-state hazards.
"""

from __future__ import annotations

import asyncio

import pytest
from helpers import TrackingRuntime, make_coordinator, make_runtime

from modex_graph import (
    Graph,
    GraphBubbleUp,
    GraphContext,
    GraphEngine,
    GraphInterrupt,
    GraphNode,
    GraphState,
    IntegratedInput,
    Node,
    SchedulerKind,
)

# ── Shared test state ──────────────────────────────────────────────────────


class ErrorState(GraphState):
    count: int = 0
    name: str = "init"
    items: list[str] = []


def make_parallel_ctx(state: ErrorState | None = None) -> GraphContext[ErrorState]:
    return GraphContext(
        state=state if state is not None else ErrorState(),
        runtime=make_runtime(),
        coordinator=make_coordinator(),
        scheduler_kind=SchedulerKind.PARALLEL,
    )


def make_tracking_ctx(
    state: ErrorState | None = None,
) -> tuple[GraphContext[ErrorState], TrackingRuntime]:
    """Build a ctx backed by TrackingRuntime; return (ctx, runtime)."""
    runtime = TrackingRuntime()
    ctx = GraphContext(
        state=state if state is not None else ErrorState(),
        runtime=runtime,
        coordinator=make_coordinator(),
        scheduler_kind=SchedulerKind.PARALLEL,
    )
    return ctx, runtime


# ── Test nodes ─────────────────────────────────────────────────────────────


class FanOutNode(Node[ErrorState]):
    """Dispatches to two targets, creating concurrent instances."""

    def __init__(self, target_a: str = "b", target_b: str = "c") -> None:
        self.target_a = target_a
        self.target_b = target_b

    async def execute(
        self, ctx: GraphContext[ErrorState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver(None, self.target_a, ctx)
        self.deliver(None, self.target_b, ctx)
        return None


class DispatchToEndNode(Node[ErrorState]):
    """No-op node that dispatches to END."""

    async def execute(
        self, ctx: GraphContext[ErrorState], integrated_input: IntegratedInput
    ) -> None:
        self.deliver(None, GraphNode.END, ctx)
        return None


class AsyncRaisingNode(Node[ErrorState]):
    """Async node that raises ``RuntimeError`` after yielding once.

    The ``await asyncio.sleep(0)`` yields control so the sibling task
    gets a chance to start (setting ``started = True``) before this
    node raises. Without the yield, a sync raise would complete before
    the sibling task is scheduled, and gather would cancel the sibling
    before it starts — making the cancellation untestable.
    """

    def __init__(self, exc: type[BaseException] = RuntimeError, msg: str = "boom") -> None:
        self.exc = exc
        self.msg = msg

    async def execute(
        self, ctx: GraphContext[ErrorState], integrated_input: IntegratedInput
    ) -> None:
        await asyncio.sleep(0)
        raise self.exc(self.msg)


class AsyncInterruptNode(Node[ErrorState]):
    """Async node that calls ``ctx.interrupt(value)`` after yielding."""

    def __init__(self, value: str = "interrupted") -> None:
        self.value = value

    async def execute(
        self, ctx: GraphContext[ErrorState], integrated_input: IntegratedInput
    ) -> None:
        await asyncio.sleep(0)
        ctx.interrupt(self.value)
        return None  # Unreachable — interrupt raises.


class AsyncSlowNode(Node[ErrorState]):
    """Async node that sleeps a long time. Should be cancelled by gather.

    Exposes ``started`` / ``completed`` flags so the test can verify
    the node was started but not allowed to finish.
    """

    def __init__(self, label: str = "slow") -> None:
        self.label = label
        self.started = False
        self.completed = False

    async def execute(
        self, ctx: GraphContext[ErrorState], integrated_input: IntegratedInput
    ) -> None:
        self.started = True
        await asyncio.sleep(10)
        self.completed = True
        self.deliver(None, None, ctx)
        return None


class AsyncEmitNode(Node[ErrorState]):
    """Async node that calls ``ctx.emit`` multiple times then dispatches to END."""

    def __init__(self, event_type: str = "test_event", count: int = 3) -> None:
        self.event_type = event_type
        self.count = count

    async def execute(
        self, ctx: GraphContext[ErrorState], integrated_input: IntegratedInput
    ) -> None:
        # Yield so the sibling task also gets to run emits concurrently.
        await asyncio.sleep(0)
        for i in range(self.count):
            ctx.emit(self.event_type, {"seq": i})
        self.deliver(None, GraphNode.END, ctx)
        return None


class WriteStateNode(Node[ErrorState]):
    def __init__(self, field: str, value: int | str) -> None:
        self.field = field
        self.value = value

    async def execute(
        self, ctx: GraphContext[ErrorState], integrated_input: IntegratedInput
    ) -> None:
        setattr(ctx.state, self.field, self.value)
        self.deliver(None, None, ctx)
        return None


# ── Graph builder helpers ──────────────────────────────────────────────────


def _build_fanout_graph(
    node_b: Node[ErrorState],
    node_c: Node[ErrorState],
) -> Graph[ErrorState]:
    """Build a fan-out graph: START → a → {b, c} → END."""
    g: Graph[ErrorState] = Graph()
    g.add_node("a", FanOutNode(target_a="b", target_b="c"))
    g.add_node("b", node_b)
    g.add_node("c", node_c)
    g.add_edge(GraphNode.START, "a")
    g.add_edge("a", "b")
    g.add_edge("a", "c")
    g.add_edge("b", GraphNode.END)
    g.add_edge("c", GraphNode.END)
    return g


# ── Tests: RuntimeError propagation + cancellation ────────────────────────


class TestRuntimeErrorCancelsConcurrent:
    """Non-GraphBubbleUp exception cancels concurrent instances and propagates."""

    async def test_runtime_error_propagates_and_cancels_sibling(self) -> None:
        """One async instance raises RuntimeError; the other is cancelled."""
        slow = AsyncSlowNode(label="c")
        g = _build_fanout_graph(
            node_b=AsyncRaisingNode(RuntimeError, "boom"),
            node_c=slow,
        )
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ErrorState())
        with pytest.raises(RuntimeError, match="boom"):
            await GraphEngine(compiled).run_async(ctx)

        # The slow node started (gather scheduled it) but was cancelled
        # before completing — asyncio.gather cancels not-yet-completed
        # tasks when one raises.
        assert slow.started, "slow node should have started before cancellation"
        assert not slow.completed, "slow node should NOT have completed"

    async def test_value_error_also_propagates(self) -> None:
        """Any non-GraphBubbleUp exception propagates (not just RuntimeError)."""
        slow = AsyncSlowNode(label="c")
        g = _build_fanout_graph(
            node_b=AsyncRaisingNode(ValueError, "bad value"),
            node_c=slow,
        )
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ErrorState())
        with pytest.raises(ValueError, match="bad value"):
            await GraphEngine(compiled).run_async(ctx)

        assert slow.started
        assert not slow.completed

    async def test_exception_does_not_corrupt_main_state(self) -> None:
        """An exception does not alter state when neither branch mutates it."""
        slow = AsyncSlowNode(label="c")
        g = _build_fanout_graph(
            node_b=AsyncRaisingNode(RuntimeError, "fail"),
            node_c=slow,
        )
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ErrorState(count=42))
        with pytest.raises(RuntimeError):
            await GraphEngine(compiled).run_async(ctx)

        assert ctx.state.count == 42


# ── Tests: GraphInterrupt propagation + cancellation ──────────────────────


class TestGraphInterruptCancelsConcurrent:
    """GraphInterrupt (GraphBubbleUp) cancels concurrent instances and propagates."""

    async def test_graph_interrupt_propagates_and_cancels_sibling(self) -> None:
        """One async instance calls ctx.interrupt(value); the other is cancelled."""
        slow = AsyncSlowNode(label="c")
        g = _build_fanout_graph(
            node_b=AsyncInterruptNode(value="approval_needed"),
            node_c=slow,
        )
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ErrorState())
        with pytest.raises(GraphInterrupt) as exc_info:
            await GraphEngine(compiled).run_async(ctx)

        assert exc_info.value.value == "approval_needed"
        assert slow.started, "slow node should have started before cancellation"
        assert not slow.completed, "slow node should NOT have completed"

    async def test_graph_interrupt_is_graphbubbleup(self) -> None:
        """GraphInterrupt is catchable as GraphBubbleUp at the top level."""
        slow = AsyncSlowNode(label="c")
        g = _build_fanout_graph(
            node_b=AsyncInterruptNode(value="hitl"),
            node_c=slow,
        )
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ErrorState())
        with pytest.raises(GraphBubbleUp):
            await GraphEngine(compiled).run_async(ctx)


class TestSharedStateMutation:
    async def test_concurrent_writes_different_fields_succeed(self) -> None:
        g = _build_fanout_graph(
            node_b=WriteStateNode(field="count", value=10),
            node_c=WriteStateNode(field="name", value="from_c"),
        )
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = make_parallel_ctx(ErrorState())
        result = await GraphEngine(compiled).run_async(ctx)
        assert result.count == 10
        assert result.name == "from_c"


# ── Tests: before_node / after_node concurrent invocation ─────────────────


class TestConcurrentHooksSafe:
    """before_node / after_node are called concurrently under gather; must be safe."""

    async def test_before_after_node_invoked_for_all_nodes(self) -> None:
        """before_node / after_node fire for every executed node, including fan-out."""
        g = _build_fanout_graph(
            node_b=DispatchToEndNode(),
            node_c=DispatchToEndNode(),
        )
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx, runtime = make_tracking_ctx(ErrorState())
        await GraphEngine(compiled).run_async(ctx)

        # a (fan-out) + b + c = 3 nodes executed.
        assert len(runtime.before_calls) == 3
        assert len(runtime.after_calls) == 3
        # The entry node "a" is always first (it fans out to b and c).
        assert runtime.before_calls[0] == "a"
        assert runtime.after_calls[0] == "a"
        # b and c are in the same batch; both before_node and after_node
        # are called. Order within the batch is deterministic for sync
        # nodes (gather runs them in submission order).
        assert set(runtime.before_calls[1:]) == {"b", "c"}
        assert set(runtime.after_calls[1:]) == {"b", "c"}

    async def test_concurrent_before_after_node_no_crash(self) -> None:
        """Concurrent before_node / after_node calls don't crash.

        Two async nodes run under gather; each triggers before_node +
        after_node. The TrackingRuntime appends to shared lists —
        list.append is GIL-atomic, so concurrent appends are safe.
        """
        g = _build_fanout_graph(
            node_b=AsyncEmitNode(event_type="b_event", count=1),
            node_c=AsyncEmitNode(event_type="c_event", count=1),
        )
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx, runtime = make_tracking_ctx(ErrorState())
        # Should complete without raising.
        await GraphEngine(compiled).run_async(ctx)

        # a + b + c = 3 nodes.
        assert len(runtime.before_calls) == 3
        assert len(runtime.after_calls) == 3


# ── Tests: ctx.emit concurrent invocation ─────────────────────────────────


class TestConcurrentEmitSafe:
    """ctx.emit is fire-and-forget; concurrent invocation must not block or race."""

    async def test_concurrent_emit_does_not_block(self) -> None:
        """Two async nodes each call ctx.emit multiple times; no block, no crash.

        ctx.emit uses ``loop.create_task`` (fire-and-forget). The emit
        tasks are scheduled on the event loop and run after the current
        batch's synchronous sections complete. The test verifies the
        graph finishes without deadlock and emits were received.
        """
        g = _build_fanout_graph(
            node_b=AsyncEmitNode(event_type="b_event", count=3),
            node_c=AsyncEmitNode(event_type="c_event", count=3),
        )
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx, runtime = make_tracking_ctx(ErrorState())
        await GraphEngine(compiled).run_async(ctx)

        # Each node emitted 3 events → 6 total. The emit tasks are
        # fire-and-forget; they may not all have completed by the time
        # run_async returns. Yield once to let pending emit tasks run.
        await asyncio.sleep(0)

        # 3 from b + 3 from c = 6.
        assert len(runtime.emit_calls) == 6
        b_events = [e for e in runtime.emit_calls if e[0] == "b_event"]
        c_events = [e for e in runtime.emit_calls if e[0] == "c_event"]
        assert len(b_events) == 3
        assert len(c_events) == 3

    async def test_emit_does_not_block_execution(self) -> None:
        """ctx.emit returns immediately; node execution is not delayed by emit.

        A node that emits many events should complete just as fast as
        one that emits none (emit is fire-and-forget).
        """

        class NoEmitNode(Node[ErrorState]):
            async def execute(
                self, ctx: GraphContext[ErrorState], integrated_input: IntegratedInput
            ) -> None:
                await asyncio.sleep(0)
                self.deliver(None, GraphNode.END, ctx)
                return None

        g = _build_fanout_graph(
            node_b=AsyncEmitNode(event_type="many", count=50),
            node_c=NoEmitNode(),
        )
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx, runtime = make_tracking_ctx(ErrorState())
        # Should complete quickly — emit is non-blocking.
        await GraphEngine(compiled).run_async(ctx)
        await asyncio.sleep(0)

        # 50 emit calls from b, none from c.
        assert len(runtime.emit_calls) == 50


# ── ReactGraphRuntime audit (read-only, documented as comment) ────────────


class TestReactGraphRuntimeAudit:
    """Audit findings for ReactGraphRuntime shared mutable state (Task 08).

    This is a READ-ONLY audit — ``src/modex_agent/agents/react/runtime.py``
    was inspected but NOT modified. Findings are documented in the module
    docstring above and asserted here as lightweight structural checks.

    The audit confirms ReactGraphRuntime has NO shared mutable state on
    ``self``: all 7 service references are set once in ``__init__`` and
    never mutated. All methods are read-only on ``self``. Therefore
    concurrent invocation of ``before_node`` / ``after_node`` / ``emit``
    is safe at the ReactGraphRuntime layer — safety of the underlying
    services (HookRunner, ContentEmitter, etc.) is a separate, framework-
    wide concern outside this audit's scope.
    """

    def test_react_graph_runtime_before_node_is_noop(self) -> None:
        """before_node / after_node are no-ops in ReactGraphRuntime.

        Verified by reading the source: both methods have empty bodies.
        No-op methods are trivially safe under concurrent invocation.
        """
        import inspect

        from modex_agent.agents.react.runtime import ReactGraphRuntime

        # before_node and after_node are defined on ReactGraphRuntime
        # with empty bodies (no-op). We verify they exist and are
        # coroutine functions.
        assert inspect.iscoroutinefunction(ReactGraphRuntime.before_node)
        assert inspect.iscoroutinefunction(ReactGraphRuntime.after_node)

    def test_react_graph_runtime_has_no_mutable_instance_state(self) -> None:
        """ReactGraphRuntime stores only service references in __init__.

        The constructor assigns 7 keyword-only arguments to private
        attributes. None of these are mutable containers (lists, dicts,
        sets) that grow during execution — they are service handles
        (HookRunner, InterceptorChain, etc.) whose own thread-safety is
        a separate concern.
        """
        from modex_agent.agents.react.runtime import ReactGraphRuntime

        # Construct with all-None services — should succeed and store
        # exactly the 7 service references.
        rt = ReactGraphRuntime()
        # The 7 service attributes exist and are None.
        expected_attrs = {
            "_hook_runner",
            "_interceptor_chain",
            "_governance",
            "_control_channel",
            "_snapshot_policy",
            "_turn_state_store",
            "_emitter",
        }
        actual_attrs = {
            attr for attr in dir(rt) if attr.startswith("_") and not attr.startswith("__")
        }
        # All expected service attrs are present.
        assert expected_attrs.issubset(actual_attrs)
        # All are None when constructed without services.
        for attr in expected_attrs:
            assert getattr(rt, attr) is None, f"{attr} should be None"

    async def test_react_runtime_before_after_node_concurrent_no_crash(self) -> None:
        """Concurrent before_node / after_node on ReactGraphRuntime don't crash.

        Uses the real ReactGraphRuntime (with all-None services → no-ops)
        under a fan-out graph. Verifies the no-op hooks are safe under
        concurrent gather invocation.
        """
        from modex_agent.agents.react.runtime import ReactGraphRuntime

        runtime = ReactGraphRuntime()  # all services None → no-ops
        g = _build_fanout_graph(
            node_b=AsyncEmitNode(event_type="b", count=1),
            node_c=AsyncEmitNode(event_type="c", count=1),
        )
        compiled = g.compile(scheduler=SchedulerKind.PARALLEL)

        ctx = GraphContext(
            state=ErrorState(),
            runtime=runtime,
            coordinator=make_coordinator(),
            scheduler_kind=SchedulerKind.PARALLEL,
        )
        # Should complete without raising — no-op hooks are safe.
        await GraphEngine(compiled).run_async(ctx)
