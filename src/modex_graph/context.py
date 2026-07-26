# ruff: noqa: ANN401

"""`GraphContext[S]` — node execution context.

Per ADR-0033 D5.1: a regular class (NOT Pydantic — it holds runtime objects
per rule 12). Subclassable so business modules can add type-safe accessors
(e.g. `ReActGraphContext` with `agent_ctx` / `tool_manager` /
`context_manager` properties).

Provides:

- `state: S` — the typed Pydantic `GraphState` instance. Nodes read/write
  directly (`ctx.state.x = y` for imperative; `ctx.state.apply_state_update`
  is called by the engine for declarative `NodeResult.state_update`).
- `runtime: GraphRuntime` — AOP bridge. Nodes call `ctx.runtime.dispatch_hook`
  etc. for business-specific AOP.
- `user_data: Any` — turn-scoped business context. For ReAct, holds the
  `AgentContext`. Shared across forks.
- `fork(state=..., parent=...)` — create a sub-context for `Task` fan-out.
  See `fork` docstring for shared/isolated semantics.
- `emit(event_type, data)` — convenience for `ctx.runtime.emit(..., ctx)`.
- `interrupt(value)` — raises `GraphInterrupt(value)` (suspend-without-
  re-execution semantics).
- `dispatch(target, state_update)` — under `ParallelScheduler`, routes to a
  downstream node by creating a `DispatchEvent` and queuing a new
  `NodeInstance`. Under `LinearScheduler`, raises `RuntimeError`.

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
    from .state import GraphState

S = TypeVar("S", bound="GraphState")

# Dispatch handler signature: (source_instance, target, payload) -> None.
# Provided by `ParallelScheduler` via `set_dispatch_handler`. Raises
# `RoutingError` if `target` is not in the source node's outgoing edges.
type DispatchHandler = Callable[[str, str, "dict[str, Any] | None"], None]


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

    Construction: `GraphContext(state=..., runtime=..., user_data=...)`.
    The engine constructs the initial context; `fork()` creates sub-contexts.
    """

    def __init__(
        self,
        *,
        state: S,
        runtime: GraphRuntime,
        user_data: Any = None,
        scheduler_kind: SchedulerKind = SchedulerKind.LINEAR,
        dispatch_handler: DispatchHandler | None = None,
        current_instance: str | None = None,
    ) -> None:
        self.state: S = state
        self.runtime: GraphRuntime = runtime
        self.user_data: Any = user_data
        self.scheduler_kind: SchedulerKind = scheduler_kind
        self._dispatch_handler: DispatchHandler | None = dispatch_handler
        self._current_instance: str | None = current_instance

    def fork(
        self,
        *,
        state: S | None = None,
        runtime: GraphRuntime | None = None,
        user_data: Any = None,
        scheduler_kind: SchedulerKind | None = None,
        dispatch_handler: DispatchHandler | None = None,
        current_instance: str | None = None,
    ) -> GraphContext[S]:
        """Create a sub-context for `Task` fan-out. Three layers of sharing.

        Per ADR-0033 D5.2:

        - **`runtime` shared** (inherited from parent if `runtime=None`):
          subtask uses the same AOP services (hook_runner / emitter /
          snapshot_store). AOP services are turn-scoped, not task-scoped.
        - **`user_data` shared** (inherited from parent if `user_data=None`):
          subtask sees the same `AgentContext` (or business context).
          Turn-internal context does not change across tasks.
        - **`state` isolated** (if `state` is passed): subtask has its own
          state. Imperative mutations (`sub_ctx.state.x = y`) do NOT
          propagate to the parent state. Only `NodeResult.state_update`
          is merged back to the parent via reducer channels.
          If `state=None` is passed (the default), the subtask shares the
          parent state (mutations propagate directly — use with care under
          `LinearScheduler`; `ParallelScheduler` forbids this via fork
          isolation, ADR-0034 D7).
        - **`scheduler_kind` shared** (inherited from parent if
          `scheduler_kind=None`): subtask sees the same scheduler kind.
          Needed so `dispatch` checks the right kind under fan-out.
        - **`dispatch_handler` shared** (inherited from parent if
          `dispatch_handler=None`): a forked context under `PARALLEL`
          retains the parent's dispatch handler so it can also dispatch.
          Under `LINEAR`, both parent and fork have `None` (dispatch raises
          `RuntimeError`).
        - **`current_instance` NOT inherited** (defaults to `None` unless
          explicitly passed): a forked context is a different execution
          context; it should not claim the parent's instance identity. The
          `ParallelScheduler` sets `current_instance` explicitly per
          instance execution via `set_current_instance`.
        """
        return GraphContext(
            state=state if state is not None else self.state,
            runtime=runtime if runtime is not None else self.runtime,
            user_data=user_data if user_data is not None else self.user_data,
            scheduler_kind=scheduler_kind if scheduler_kind is not None else self.scheduler_kind,
            dispatch_handler=dispatch_handler
            if dispatch_handler is not None
            else self._dispatch_handler,
            current_instance=current_instance,
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
        """Route to a downstream node under `ParallelScheduler`.

        Validates `target` against the current node's outgoing edges (via the
        dispatch handler), creates a `DispatchEvent`, and queues a new
        `NodeInstance` for the target. Dispatch takes effect immediately —
        the handler runs synchronously inside this call.

        Under `LinearScheduler` (or any non-`PARALLEL` scheduler kind),
        raises `RuntimeError`. The dispatch handler is only registered by
        `ParallelScheduler`; if `scheduler_kind` is `PARALLEL` but no handler
        is registered (programmer error), raises `RuntimeError`.

        Args:
            target: The target node name, or `GraphNode.END` for the
                terminal signal.
            state_update: Optional payload carried by the `DispatchEvent`.
                Applied to the target instance's state in future
                fork-isolation phases; currently logged for audit.

        Raises:
            RuntimeError: If `scheduler_kind` is not `PARALLEL`, or if no
                dispatch handler is registered under `PARALLEL`.
            RoutingError: If `target` is not in the current node's outgoing
                edges.
        """
        if self.scheduler_kind != SchedulerKind.PARALLEL:
            raise RuntimeError("dispatch is only available under ParallelScheduler")
        if self._dispatch_handler is None:
            raise RuntimeError(
                "dispatch called under PARALLEL but no dispatch_handler is "
                "registered. ParallelScheduler must set the handler before "
                "executing nodes."
            )
        self._dispatch_handler(self._current_instance or "", target, state_update)

    def set_dispatch_handler(self, handler: DispatchHandler | None) -> None:
        """Register the dispatch handler. Called by `ParallelScheduler`.

        The handler is a callback with signature
        `(source_instance: str, target: str, payload: dict | None) -> None`.
        It validates the target, creates a `DispatchEvent`, and queues the
        target instance. Passing `None` clears the handler.
        """
        self._dispatch_handler = handler

    def set_current_instance(self, instance_id: str | None) -> None:
        """Set the currently-executing instance ID. Called by `ParallelScheduler`.

        The dispatch handler uses this to identify the source of each
        dispatch. Set to `None` to clear (e.g. between executions).
        """
        self._current_instance = instance_id


__all__ = ["GraphContext", "S", "DispatchHandler"]
