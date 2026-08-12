# ruff: noqa: ANN401

"""`GraphContext[S]` — node execution context.

Per ADR-0033 D5.1: a regular class (NOT Pydantic — it holds runtime objects
per rule 12). Subclassable so business modules can add type-safe accessors
(e.g. `ReActGraphContext` with `agent_ctx` / `tool_manager` /
`context_manager` properties).

Provides:

- `state: S` — the typed Pydantic `GraphState` instance. Nodes read and write
  it directly (`ctx.state.x = y`). Per-node working state is accessed via
  `ctx.scratch` (scoped to `ctx.state.node_scratch[current_node_id]`).
- `runtime: GraphRuntime` — AOP bridge. Nodes call `ctx.runtime.dispatch_hook`
  etc. for business-specific AOP.
- `user_data: Any` — turn-scoped business context. For ReAct, holds the
  `AgentContext`.
- `emit(event_type, data)` — convenience for `ctx.runtime.emit(..., ctx)`.
- `interrupt(value)` — raises `GraphInterrupt(value)` (suspend-without-
  re-execution semantics).
- `dispatch(target, state_update)` — routes to a downstream node. Under
  `ParallelScheduler`, validates and queues the target according to its
  trigger mode. Under `LinearScheduler`, records the target + payload for
  the scheduler to pick up as the next node. Both schedulers register a
  dispatch handler before executing nodes.

`S` is bound to `GraphState`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NoReturn

from typing_extensions import TypeVar

from .constants import SchedulerKind
from .exceptions import GraphInterrupt
from .execution_context import get_execution
from .integration import GraphPayload
from .run_control import GraphRunControl
from .runtime import GraphRuntime

if TYPE_CHECKING:
    from .persistence import GraphPersistenceCoordinator, NodeStateStore
    from .state import GraphState

S = TypeVar("S", bound="GraphState")

# Dispatch handler signature: (source_instance, target, state_update) -> None.
# Provided by BOTH `ParallelScheduler` and `LinearScheduler` via
# `set_dispatch_handler`. Under PARALLEL, the handler routes the deliver and
# queues a new `NodeInstance` according to its trigger mode. Under LINEAR, the
# handler records the target + payload for the scheduler to pick up as the next
# node. Both schedulers validate that `target` is in the source node's outgoing
# edges (topology enforcement via `_dispatch_utils.validate_dispatch_target`).
type DispatchHandler = Callable[[str, str, "dict[str, Any] | None"], None]


def _noop_dispatch_handler(
    source_instance: str,
    target: str,
    state_update: dict[str, Any] | None,
) -> None:
    """Default no-op dispatch handler.

    Used when no scheduler is active (e.g. direct ``node.run(ctx)`` calls
    in tests). Both schedulers overwrite this with their own handler via
    ``set_dispatch_handler`` before executing nodes. Call
    ``set_dispatch_handler(None)`` to explicitly clear the handler —
    ``dispatch()`` then raises ``RuntimeError`` (programmer error).
    """


class GraphContext[S: "GraphState"]:
    """Node execution context. Regular class (NOT Pydantic) — subclassable.

    Per D5.1: business modules subclass this to add type-safe accessors:

    ```python
    class ReActGraphContext(GraphContext[ReActTurnState]):
        @property
        def agent_ctx(self) -> AgentContext:
            return self.user_data
        @property
        def tool_manager(self) -> ToolManager:
            return self.agent_ctx.runtime.services.tool_manager
    ```

    Construction: `GraphContext(state=..., runtime=..., coordinator=..., ...)`.
    The engine constructs the initial context.

    ``coordinator`` is REQUIRED (no None fallback). It drives
    the node invocation lifecycle (begin/integrate/complete/cancel/suspend/
    crash/finalize) via ``Node.run()``.
    """

    def __init__(
        self,
        *,
        state: S,
        runtime: GraphRuntime,
        coordinator: GraphPersistenceCoordinator,
        user_input: GraphPayload | None = None,
        user_data: Any = None,
        scheduler_kind: SchedulerKind = SchedulerKind.LINEAR,
        dispatch_handler: DispatchHandler | None = None,
        graph_instance_id: int | None = None,
        control: GraphRunControl | None = None,
        reached_end: bool = False,
    ) -> None:
        self.state: S = state
        self.runtime: GraphRuntime = runtime
        self.coordinator: GraphPersistenceCoordinator = coordinator
        self.user_input: GraphPayload | None = user_input
        self.user_data: Any = user_data
        self.scheduler_kind: SchedulerKind = scheduler_kind
        self._dispatch_handler: DispatchHandler | None = (
            dispatch_handler if dispatch_handler is not None else _noop_dispatch_handler
        )
        self.graph_instance_id: int | None = graph_instance_id
        self.control: GraphRunControl = control if control is not None else GraphRunControl()
        # Run-level flag: set True when a dispatch targets GraphNode.END.
        # The orchestrator reads this after run_async returns to decide
        # COMPLETED (reached END) vs FAILED (dead-end — no dispatches
        # produced, graph did not reach END).
        self.reached_end: bool = reached_end

    def emit(self, event_type: str, data: Any) -> None:
        """Fire-and-forget emit via `runtime.emit(..., ctx)`.

        Schedules the async `runtime.emit` on the current event loop without
        awaiting. For ordered emits (where order matters), call
        `await ctx.runtime.emit(event_type, data, ctx)` directly.

        If no event loop is running, this is a no-op (the caller should use
        the async form directly in that case).
        """
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.runtime.emit(event_type, data, self))
        except RuntimeError:
            pass

    def interrupt(self, value: Any = None) -> NoReturn:
        """Raise `GraphInterrupt(value)` to suspend graph execution.

        Suspend-without-re-execution semantics: already-applied state
        updates and side effects persist across the interrupt boundary.
        Resume re-enters the graph at the entry node; the interrupted node
        body is NOT re-run.

        The caller is responsible for setting `state.resume_target` before
        calling this (typically before capturing a snapshot) so the entry
        node can route to the resume target on re-entry.

        `value` is the interrupt payload (e.g. an `ApprovalTransaction`
        awaiting human decision). The caller inspects `value` to determine
        resume semantics.
        """
        raise GraphInterrupt(value=value)

    def dispatch(self, target: str, state_update: dict[str, Any] | None = None) -> None:
        """Route to a downstream node via the registered dispatch handler.

        Works under BOTH `LINEAR` and `PARALLEL` schedulers. Both
        schedulers register a dispatch handler before executing nodes:

        - Under `ParallelScheduler`: the handler validates `target` against
          the current node's outgoing edges, routes the deliver through the
          coordinator, and queues the target according to its trigger mode.
        - Under `LinearScheduler`: the handler records the target + payload
          for the scheduler to pick up as the next node (LINEAR is
          sequential — one target at a time).

        Dispatch takes effect immediately — the handler runs synchronously
        inside this call.

        Args:
            target: The target node name, or `GraphNode.END` for the
                terminal signal.
            state_update: Optional payload carried by the dispatch. Under
                both schedulers, `{"delivered": content}` is the conventional
                shape — the downstream node receives it as an
                `IntegratedPayload` via the coordinator's deliver consumption
                in `node.run()`.

        Raises:
            RuntimeError: If no dispatch handler is registered (programmer
                error — the scheduler must set the handler before executing
                nodes).
            RoutingError: (PARALLEL only) If `target` is not in the current
                node's outgoing edges.
        """
        if self._dispatch_handler is None:
            raise RuntimeError(
                "dispatch called but no dispatch_handler is registered. "
                "The scheduler must set the handler before executing nodes."
            )
        source_instance = self._current_instance or ""
        self._dispatch_handler(source_instance, target, state_update)

    def set_dispatch_handler(self, handler: DispatchHandler | None) -> None:
        """Register the dispatch handler. Called by both schedulers.

        The handler is a callback with signature
        `(source_instance: str, target: str, state_update: dict | None) -> None`.
        Under `ParallelScheduler`, it validates the target, routes the deliver,
        and queues the target according to its trigger mode. Under
        `LinearScheduler`, it records the target + payload for the scheduler to
        pick up as the next node. Passing `None` clears the handler.
        """
        self._dispatch_handler = handler

    @property
    def _current_instance(self) -> str | None:
        """The currently-executing instance ID (read-only).

        Delegates to the invocation-local execution context (ContextVar,
        task-local). Returns None when no node is executing in the current
        task. Set by the scheduler via ``set_execution()`` — never
        write to this property directly.
        """
        exec_ctx = get_execution()
        return exec_ctx.instance_id if exec_ctx is not None else None

    @property
    def node_state_store(self) -> NodeStateStore:
        """The node state store (lifecycle + version chain + CAS authority).

        Convenience accessor for ``self.coordinator.node_state_store``.
        ``Node.run()`` calls lifecycle methods (begin / complete / suspend /
        crash / cancel / finalize) through this property.
        """
        return self.coordinator.node_state_store

    @property
    def scratch(self) -> dict[str, Any]:
        """The current node's scoped working-state region.

        Returns ``ctx.state.node_scratch[current_node_id]`` — the
        per-node scratch dict where the currently-executing node writes
        its local working state. Key separation from other nodes provides
        isolation without context copying.

        Resolves ``current_node_id`` from the invocation-local execution
        context (ContextVar, task-local). Raises ``RuntimeError`` if no
        invocation is active.

        Example::

            ctx.scratch["attempt"] = 3
        """
        exec_ctx = get_execution()
        inv = exec_ctx.invocation if exec_ctx is not None else None
        if inv is None:
            raise RuntimeError(
                "ctx.scratch is only valid during node execution "
                "(no active invocation). Use inside execute()/deliver()."
            )
        scratch = self.state.node_scratch
        if inv.node_id not in scratch:
            scratch[inv.node_id] = {}
        return scratch[inv.node_id]


__all__ = ["GraphContext", "S", "DispatchHandler"]
