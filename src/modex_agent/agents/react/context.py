"""``ReActGraphContext`` — type-safe ``GraphContext`` subclass for ReAct.

Per ADR-0033 D5.1: business modules subclass ``GraphContext[S]`` to add
type-safe accessors (``agent_ctx`` / ``tool_manager`` / ``context_manager``)
instead of ``cast(AgentContext, ctx.user_data)`` at every access site.

Stage 3 status (ticket 04): defined but NOT used yet. The current ticket
migrates AOP calls in nodes to ``ctx.runtime.graph_runtime.*`` but keeps
the old ``core/graph/`` engine, which passes ``AgentContext`` (not
``GraphContext``) to ``Node.execute``. Nodes construct thin
``GraphContext`` wrappers per call. Ticket 05 switches to the new
``modex_graph`` engine and ``ReActGraphContext`` becomes the actual
execution context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from modex_graph.context import GraphContext

from .state import ReActTurnState

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.runtime_context import RuntimeContextManager
    from modex_agent.core.tool_manager import ToolManager


class ReActGraphContext(GraphContext[ReActTurnState]):
    """``GraphContext[ReActTurnState]`` with type-safe ReAct accessors.

    Wraps an ``AgentContext`` in ``user_data`` and exposes typed properties
    for the framework services ReAct nodes need. Eliminates
    ``cast(AgentContext, ctx.user_data)`` boilerplate.

    NOT used in ticket 04 — nodes construct plain ``GraphContext`` wrappers.
    Ticket 05 (engine switch) constructs ``ReActGraphContext`` as the
    execution context passed to ``Node.execute``.
    """

    @property
    def agent_ctx(self) -> AgentContext:
        return cast(AgentContext, self.user_data)

    @property
    def tool_manager(self) -> ToolManager | None:
        return self.agent_ctx.tool_manager

    @property
    def runtime_context_manager(self) -> RuntimeContextManager | None:
        runtime = self.agent_ctx.runtime
        return runtime.services.runtime_context_manager if runtime is not None else None


__all__ = ["ReActGraphContext"]
