"""Shared test state types + node helpers for modex_graph unit tests."""
from __future__ import annotations

from typing import Annotated, Any

from modex_graph import (
    GraphContext,
    GraphRuntime,
    GraphState,
    IntegratedInput,
    LastValue,
    Node,
    NodeResult,
    ReducerChannel,
)


class TrackingRuntime(GraphRuntime):
    """Runtime that records ``before_node`` / ``after_node`` / ``emit`` calls.

    Used by Task 08 tests to verify concurrent hook invocation is safe
    (no crash, no race). The lists use ``list.append`` which is GIL-atomic
    in CPython, so concurrent ``asyncio.gather`` tasks can append safely
    without an explicit lock — the append runs in a synchronous section
    between await points.
    """

    def __init__(self) -> None:
        self.before_calls: list[str] = []
        self.after_calls: list[str] = []
        self.emit_calls: list[tuple[str, Any]] = []

    async def before_node(self, ctx: GraphContext[Any], node_name: str) -> None:
        self.before_calls.append(node_name)

    async def after_node(
        self, ctx: GraphContext[Any], node_name: str, result: Any
    ) -> None:
        self.after_calls.append(node_name)

    async def emit(
        self, event_type: str, data: Any, ctx: GraphContext[Any]
    ) -> None:
        self.emit_calls.append((event_type, data))


class CounterState(GraphState):
    """Simple state with a counter + message list for testing."""

    count: Annotated[int, LastValue] = 0
    name: Annotated[str, LastValue] = ""
    messages: Annotated[list[str], ReducerChannel(reducer=lambda a, b: a + b)] = []


class AddNode(Node[CounterState]):
    """Sync node that increments count by `amount`, delivers to default target."""

    def __init__(self, amount: int = 1) -> None:
        self.amount = amount

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        ctx.state.count += self.amount
        self.deliver(None, None, ctx)
        return NodeResult()


class AsyncAddNode(Node[CounterState]):
    """Async node that increments count by `amount`, delivers to default target."""

    def __init__(self, amount: int = 1) -> None:
        self.amount = amount

    async def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        ctx.state.count += self.amount
        self.deliver(None, None, ctx)
        return NodeResult()


class InterruptNode(Node[CounterState]):
    """Node that calls ctx.interrupt(value) to suspend."""

    def __init__(self, value: Any = "interrupted") -> None:
        self.value = value

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        ctx.interrupt(self.value)
        # Unreachable — interrupt raises.
        return NodeResult()


class RecordNameNode(Node[CounterState]):
    """Node that records its name into state.messages via state_update."""

    def __init__(self, label: str | None = None) -> None:
        self.label = label

    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        label = self.label if self.label is not None else self.name
        return NodeResult(state_update={"messages": [label]})


def make_runtime() -> GraphRuntime:
    """Return a default no-op GraphRuntime."""
    return GraphRuntime()


def make_ctx(state: CounterState | None = None) -> GraphContext[CounterState]:
    """Build a GraphContext with a CounterState + no-op runtime.

    Registers a no-op dispatch handler so ``Node._submit`` can call
    ``ctx.dispatch()`` without a RuntimeError. Tests that need to verify
    dispatch calls should register their own recording handler via
    ``ctx.set_dispatch_handler(...)`` (overwrites the no-op).
    """
    ctx = GraphContext(
        state=state if state is not None else CounterState(),
        runtime=make_runtime(),
    )
    # No-op dispatch handler — required because _submit always calls
    # ctx.dispatch (rule 15 convergence: no scheduler_kind branch).
    ctx.set_dispatch_handler(lambda _src, _tgt, _update: None)
    return ctx
