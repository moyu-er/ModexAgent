# ruff: noqa: ANN401

"""`GraphContext[S]` — node execution context.

Per ADR-0033 D5.1: a regular class (NOT Pydantic — it holds runtime objects
per rule 12). Subclassable so business modules can add type-safe accessors
(e.g. `ReActGraphContext` with `agent_ctx` / `tool_manager` /
`context_manager` properties).

Provides:

- `state: S` — the typed Pydantic `GraphState` instance. Nodes read and write
  it directly (`ctx.state.x = y`).
- `runtime: GraphRuntime` — AOP bridge. Nodes call `ctx.runtime.dispatch_hook`
  etc. for business-specific AOP.
- `user_data: Any` — turn-scoped business context. For ReAct, holds the
  `AgentContext`. Shared across forks.
- `fork(state=..., parent=...)` — create a sub-context with isolated state.
  See `fork` docstring for shared/isolated semantics.
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
from .runtime import GraphRuntime

if TYPE_CHECKING:
    from .persistence import GraphPersistenceCoordinator, InvocationContext, NodeStateStore
    from .state import GraphState

S = TypeVar("S", bound="GraphState")

# Dispatch handler signature: (source_instance, target, state_update) -> None.
# Provided by BOTH `ParallelScheduler` and `LinearScheduler` via
# `set_dispatch_handler`. Under PARALLEL, the handler routes the deliver and
# queues a new `NodeInstance` according to its trigger mode. Under LINEAR, the
# handler records the target + payload for the scheduler to pick up as the next
# node. Raises `RoutingError` if `target` is not in the source node's outgoing
# edges (PARALLEL only — LINEAR does not validate edges).
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
    The engine constructs the initial context; `fork()` creates sub-contexts.

    ``coordinator`` is REQUIRED (no None fallback). It drives
    the node invocation lifecycle (begin/integrate/complete/cancel/suspend/
    crash/finalize) via ``Node.run()``. ``current_invocation`` is set by
    ``Node.run()`` step 1 to the current ``InvocationContext``; it is ``None``
    until a node begins executing.
    """

    def __init__(
        self,
        *,
        state: S,
        runtime: GraphRuntime,
        coordinator: GraphPersistenceCoordinator,
        user_data: Any = None,
        scheduler_kind: SchedulerKind = SchedulerKind.LINEAR,
        dispatch_handler: DispatchHandler | None = None,
        current_instance: str | None = None,
        graph_instance_id: int | None = None,
        current_invocation: InvocationContext | None = None,
    ) -> None:
        self.state: S = state
        self.runtime: GraphRuntime = runtime
        # Coordinator: deliver routing + recovery queries. Lifecycle is
        # on ctx.node_state_store (property delegating to coordinator).
        self.coordinator: GraphPersistenceCoordinator = coordinator
        self.user_data: Any = user_data
        self.scheduler_kind: SchedulerKind = scheduler_kind
        # Default to the no-op handler so direct node.run(ctx) calls in
        # tests work without a scheduler. Both schedulers overwrite this
        # via set_dispatch_handler. Pass dispatch_handler=None and the
        # no-op is still installed; call set_dispatch_handler(None) to
        # explicitly clear (dispatch then raises RuntimeError).
        self._dispatch_handler: DispatchHandler | None = (
            dispatch_handler if dispatch_handler is not None else _noop_dispatch_handler
        )
        self._current_instance: str | None = current_instance
        # Graph instance ID for deliver persistence. None = no persistence
        # (in-memory accumulation). Set by the scheduler when wiring deliver/submit
        # into the execution loop.
        self.graph_instance_id: int | None = graph_instance_id
        # Current invocation context, set by Node.run() step 1.
        # None until a node begins executing.
        self.current_invocation: InvocationContext | None = current_invocation

    def fork(
        self,
        *,
        state: S | None = None,
        runtime: GraphRuntime | None = None,
        coordinator: GraphPersistenceCoordinator | None = None,
        user_data: Any = None,
        scheduler_kind: SchedulerKind | None = None,
        dispatch_handler: DispatchHandler | None = None,
        current_instance: str | None = None,
        graph_instance_id: int | None = None,
        current_invocation: InvocationContext | None = None,
    ) -> GraphContext[S]:
        """Create a sub-context with isolated state. Three layers of sharing.

        Per ADR-0033 D5.2:

        - **`runtime` shared** (inherited from parent if `runtime=None`):
          subtask uses the same AOP services (hook_runner / emitter /
          snapshot_store). AOP services are turn-scoped, not task-scoped.
        - **`coordinator` shared** (inherited from parent if
          `coordinator=None`): a forked sub-context is part of the
          same graph run, so it shares the same persistence coordinator.
          Pass an explicit `coordinator` only to override (rare).
        - **`user_data` shared** (inherited from parent if `user_data=None`):
          subtask sees the same `AgentContext` (or business context).
          Turn-internal context does not change across tasks.
        - **`state` isolated** (if `state` is passed): subtask has its own
          state. Imperative mutations (`sub_ctx.state.x = y`) do NOT
          propagate to the parent state unless the caller propagates them.
          If `state=None` is passed (the default), the subtask shares the
          parent state and mutations propagate directly. `ParallelScheduler`
          uses per-task context shells that share the same state object.
        - **`scheduler_kind` shared** (inherited from parent if
          `scheduler_kind=None`): subtask sees the same scheduler kind.
          Needed so `dispatch` checks the right kind under fan-out.
        - **`dispatch_handler` shared** (inherited from parent if
          `dispatch_handler=None`): a forked context retains the parent's
          dispatch handler so it can also dispatch. Both `LINEAR` and
          `PARALLEL` schedulers register a handler before executing nodes.
        - **`current_instance` NOT inherited** (defaults to `None` unless
          explicitly passed): a forked context is a different execution
          context; it should not claim the parent's instance identity. The
          `ParallelScheduler` sets `current_instance` explicitly per
          instance execution via `set_current_instance`.
        - **`graph_instance_id` shared** (inherited from parent if
          `graph_instance_id=None`): a forked sub-context is part of the
          same graph run, so deliver persistence keys to the same instance.
          Pass an explicit `graph_instance_id` only to override (rare).
        - **`current_invocation` NOT inherited** (defaults to `None`): each
          forked context is a different execution scope; it should not
          claim the parent's invocation identity. `Node.run()` sets this
          on its own ctx at step 1.
        """
        return GraphContext(
            state=state if state is not None else self.state,
            runtime=runtime if runtime is not None else self.runtime,
            coordinator=coordinator if coordinator is not None else self.coordinator,
            user_data=user_data if user_data is not None else self.user_data,
            scheduler_kind=scheduler_kind if scheduler_kind is not None else self.scheduler_kind,
            dispatch_handler=dispatch_handler
            if dispatch_handler is not None
            else self._dispatch_handler,
            current_instance=current_instance,
            graph_instance_id=graph_instance_id
            if graph_instance_id is not None
            else self.graph_instance_id,
            current_invocation=current_invocation,
        )

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
        self._dispatch_handler(self._current_instance or "", target, state_update)

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

    def set_current_instance(self, instance_id: str | None) -> None:
        """Set the currently-executing instance ID. Called by `ParallelScheduler`.

        The dispatch handler uses this to identify the source of each
        dispatch. Set to `None` to clear (e.g. between executions).
        """
        self._current_instance = instance_id

    @property
    def node_state_store(self) -> NodeStateStore:
        """The node state store (lifecycle + version chain + CAS authority).

        Convenience accessor for ``self.coordinator.node_state_store``.
        ``Node.run()`` calls lifecycle methods (begin / complete / suspend /
        crash / cancel / finalize) through this property.
        """
        return self.coordinator.node_state_store


__all__ = ["GraphContext", "S", "DispatchHandler"]
