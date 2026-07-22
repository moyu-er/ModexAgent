"""Shared test state types + node helpers for modex_graph unit tests."""
from __future__ import annotations

from typing import Annotated, Any

from modex_graph import (
    GraphContext,
    GraphRuntime,
    GraphState,
    LastValue,
    Node,
    NodeResult,
    ReducerChannel,
)


class CounterState(GraphState):
    """Simple state with a counter + message list for testing."""

    count: Annotated[int, LastValue] = 0
    name: Annotated[str, LastValue] = ""
    messages: Annotated[list[str], ReducerChannel(reducer=lambda a, b: a + b)] = []


class AddNode(Node[CounterState]):
    """Sync node that increments count by `amount`."""

    def __init__(self, amount: int = 1) -> None:
        self.amount = amount

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        ctx.state.count += self.amount
        return NodeResult()


class AsyncAddNode(Node[CounterState]):
    """Async node that increments count by `amount`."""

    def __init__(self, amount: int = 1) -> None:
        self.amount = amount

    async def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        ctx.state.count += self.amount
        return NodeResult()


class TransitionNode(Node[CounterState]):
    """Sync node that returns a transition."""

    def __init__(self, transition: str) -> None:
        self.transition = transition

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        return NodeResult(transition=self.transition)


class CommandNode(Node[CounterState]):
    """Sync node that returns a Command(goto=...)."""

    def __init__(self, goto: Any) -> None:
        self.goto = goto

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        from modex_graph import Command

        return NodeResult(command=Command(goto=self.goto))


class InterruptNode(Node[CounterState]):
    """Node that calls ctx.interrupt(value) to suspend."""

    def __init__(self, value: Any = "interrupted") -> None:
        self.value = value

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        ctx.interrupt(self.value)
        # Unreachable — interrupt raises.
        return NodeResult()


class RecordNameNode(Node[CounterState]):
    """Node that records its name into state.messages via state_update."""

    def __init__(self, label: str | None = None) -> None:
        self.label = label

    def execute(self, ctx: GraphContext[CounterState]) -> NodeResult:
        label = self.label if self.label is not None else self.name
        return NodeResult(state_update={"messages": [label]})


def make_runtime() -> GraphRuntime:
    """Return a default no-op GraphRuntime."""
    return GraphRuntime()


def make_ctx(state: CounterState | None = None) -> GraphContext[CounterState]:
    """Build a GraphContext with a CounterState + no-op runtime."""
    return GraphContext(
        state=state if state is not None else CounterState(),
        runtime=make_runtime(),
    )
