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

`S` is bound to `GraphState`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

from typing_extensions import TypeVar

from .exceptions import GraphInterrupt
from .runtime import GraphRuntime

if TYPE_CHECKING:
    from .state import GraphState

S = TypeVar("S", bound="GraphState")


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
    ) -> None:
        self.state: S = state
        self.runtime: GraphRuntime = runtime
        self.user_data: Any = user_data

    def fork(
        self,
        *,
        state: S | None = None,
        runtime: GraphRuntime | None = None,
        user_data: Any = None,
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
          parent state (mutations propagate directly — use with care in
          Phase a; Phase c parallel execution forbids this).
        """
        return GraphContext(
            state=state if state is not None else self.state,
            runtime=runtime if runtime is not None else self.runtime,
            user_data=user_data if user_data is not None else self.user_data,
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


__all__ = ["GraphContext", "S"]
