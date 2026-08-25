# ruff: noqa: ANN401

"""`GraphRuntime` ABC — AOP concerns out of the node body.

Per ADR-0033 D5: a framework-defined AOP bridge with default no-op
implementations. Two layers:

- **Engine-auto-invoked (2, node-level universal):** `before_node` /
  `after_node`. These are universal graph lifecycle points — every graph
  has nodes. The engine calls them at node-entry/exit unconditionally.

- **Node-explicit (6, business-specific):** `dispatch_hook` / `around` /
  `apply_governance` / `drain_control` / `capture_snapshot` / `emit`. Nodes
  call these when they need business-specific AOP. The engine does NOT
  invoke them automatically.

CRITICAL: `before_iteration` / `after_iteration` are NOT on `GraphRuntime`.
"Iteration" is a ReAct concept (one LLM+TOOL cycle), not a universal graph
concept (linear graphs, conditional branches have no iterations). ReAct
nodes dispatch `BEFORE_ITERATION` / `AFTER_ITERATION` explicitly via
`ctx.runtime.dispatch_hook(ReActHookPoint.BEFORE_ITERATION, ctx)` at the
exact same code points as today — node-controlled, not engine-controlled.

All methods are async-only. `hook_point` / `scope` / `event_type` parameters
are `str` (business modules pass `StrEnum` values, which are `str` subclasses
and satisfy the type without engine-side imports).

`dispatch_hook`'s `data` is generic `dict[str, Any] | None` — NOT `HookPayload`.
The engine stays free of `modex_agent` types; `ReactGraphRuntime` wraps
`data` into `HookPayload` internally when calling the underlying `hook_runner`.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .context import GraphContext


class GraphRuntime(ABC):  # noqa: B024
    """Framework-defined AOP bridge. Default implementations are no-ops.

    Subclasses override the methods they need. The default no-op
    implementations make the engine runnable with zero AOP wiring —
    standalone graph users supply `GraphRuntime()` or their own subclass.

    Engine-auto-invoked methods (`before_node` / `after_node`) are called
    at every node execution. Node-explicit methods are called by node code
    when needed.
    """

    # ── Engine-auto-invoked (2, node-level universal) ──────────────────

    async def before_node(self, ctx: GraphContext[Any], node_name: str) -> None:  # noqa: B027
        """Called by the engine before each node's `execute(ctx)`.

        Universal graph lifecycle point — every graph has nodes. Use for
        node-entry observability, logging, tracing, etc.
        """
        # noqa: B027
        # Default: no-op.

    async def after_node(  # noqa: B027
        self, ctx: GraphContext[Any], node_name: str
    ) -> None:
        """Called by the engine after each node's `execute(ctx)` returns.

        Use for node-exit observability, metrics, post-execution validation,
        and similar lifecycle concerns.
        """
        # noqa: B027
        # Default: no-op.

    # ── Node-explicit (6, business-specific) ───────────────────────────

    async def dispatch_hook(  # noqa: B027
        self,
        hook_point: str,
        ctx: GraphContext[Any],
        data: dict[str, Any] | None = None,
    ) -> None:
        """Dispatch a lifecycle hook. Nodes call this when they need hook AOP.

        `hook_point` is a `str` (business modules pass `StrEnum` values like
        `ReActHookPoint.BEFORE_ITERATION`). `data` is a generic dict — NOT
        `HookPayload`. `ReactGraphRuntime` wraps `data` into `HookPayload`
        internally when bridging to `modex_agent`'s `HookRunner`.

        Iteration-level hooks (`BEFORE_ITERATION` / `AFTER_ITERATION`) are
        dispatched HERE — explicitly by the node that defines what an
        "iteration" means. The engine does NOT auto-invoke them.
        """
        # noqa: B027
        # Default: no-op.

    async def around(
        self,
        scope: str,
        ctx: GraphContext[Any],
        body: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Wrap an awaitable `body` in an interceptor chain for `scope`.

        `scope` is a `str` (business modules pass `StrEnum` values like
        `ReActScope.LLM_CALL`). The runtime implementation maps `scope` to
        the correct interceptor method and constructs the typed context
        internally. `body` is a zero-arg awaitable closure.

        Default: just await `body()` with no wrapping.
        """
        return await body()

    async def apply_governance(self, messages: list[Any], ctx: GraphContext[Any]) -> list[Any]:
        """Apply governance (filtering / rewriting) to `messages` before LLM call.

        Default: return `messages` unchanged.
        """
        return messages

    async def drain_control(self, ctx: GraphContext[Any]) -> None:  # noqa: B027
        """Drain the control channel for cancellation / injection signals.

        Default: no-op. `ReactGraphRuntime` bridges to the control channel
        and raises `AgentCancelledError` if a `CANCEL_TURN` command is pending.
        """
        # noqa: B027
        # Default: no-op.

    async def capture_snapshot(  # noqa: B027
        self, ctx: GraphContext[Any], reason: str
    ) -> None:
        """Capture a turn state snapshot for suspend/resume.

        `reason` is a `str` (e.g. `"approval_suspend"`, `"max_iterations"`).
        Default: no-op. `ReactGraphRuntime` bridges to `TurnStateStore`.
        """
        # noqa: B027
        # Default: no-op.

    async def emit(  # noqa: B027
        self, event_type: str, data: Any, ctx: GraphContext[Any]
    ) -> None:
        """Emit a streaming event.

        `event_type` is a `str` (business modules pass `StrEnum` values like
        `ReActEvent.MODEL_OUTPUT`). `data` is the event payload — typed by
        the business module. Default: no-op.
        """
        # noqa: B027
        # Default: no-op.


__all__ = ["GraphRuntime"]
