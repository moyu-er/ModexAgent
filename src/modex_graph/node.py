"""`Node[S]` ABC — single-method `execute` with structured `NodeResult`.

Per ADR-0033 D2: the `execute` method is declared as `def` (NOT `async def`).
Subclasses may override with either `def` or `async def`. The engine unifies
both via `inspect.isawaitable(result)` — if the return value is awaitable
(coroutine), the engine awaits it; otherwise it uses the value directly.

This dual-mode design (borrowed from anyio/httpx/starlette precedent) avoids
splitting the node library into `SyncNode` + `AsyncNode` and duplicating the
engine loop. The cost is one `inspect.isawaitable` call per node execution
(negligible).

`S` is bound to `GraphState` — the typed Pydantic state the node reads from
and writes to via `ctx.state`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable
from typing import TYPE_CHECKING

from typing_extensions import TypeVar

from .constants import NodeTrigger

if TYPE_CHECKING:
    from .context import GraphContext
    from .result import NodeResult
    from .state import GraphState

S = TypeVar("S", bound="GraphState")


class Node[S: "GraphState"](ABC):
    """Abstract graph node. Executes logic and returns a `NodeResult`.

    Subclasses implement `execute(ctx) -> NodeResult`. The method is declared
    as `def` (sync); subclasses MAY override with `async def` for async I/O
    (LLM calls, tool execution, network requests). The engine detects the
    return type via `inspect.isawaitable` and awaits if needed.

    Convention: each `Node` instance has a `name` attribute matching its
    registration key in the `Graph`. The `Graph.add_node(name, node)` call
    sets it; subclasses may also set it in `__init__`.

    `trigger` (Task 06) is the per-node trigger mode under
    `ParallelScheduler`. `None` means "use the compiled graph's
    `default_trigger`". Subclasses may override to force a mode.
    """

    name: str = ""
    trigger: NodeTrigger | None = None

    @abstractmethod
    def execute(self, ctx: GraphContext[S]) -> NodeResult | Awaitable[NodeResult]:
        """Execute node logic and return a `NodeResult`.

        Declared as `def` (not `async def`). Subclasses may override with
        `async def` — the engine unifies both via `inspect.isawaitable`.

        Return type is `NodeResult | Awaitable[NodeResult]` to honestly
        reflect the dual-mode design: a `def` override returns `NodeResult`
        directly; an `async def` override returns a coroutine that yields
        `NodeResult`. The engine's `inspect.isawaitable` check handles both.

        Implementations may:
        - Read/write `ctx.state` imperatively (`ctx.state.x = y`).
        - Return `NodeResult(state_update={...})` for declarative updates.
        - Return `NodeResult(transition="reason")` for static edge routing.
        - Return `NodeResult(command=Command(goto=...))` for dynamic routing.
        - Call `ctx.interrupt(value)` to suspend for HITL.
        - Call `ctx.runtime.dispatch_hook(...)` for AOP concerns.
        """
        ...


__all__ = ["Node", "S"]
