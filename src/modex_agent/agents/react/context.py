"""``ReActGraphContext`` — type-safe ``GraphContext`` subclass for ReAct.

Per ADR-0033 D5.1: business modules subclass ``GraphContext[S]`` to add
type-safe accessors (``agent_ctx`` / ``tool_manager`` / ``context_manager``)
instead of ``cast(AgentContext, ctx.user_data)`` at every access site.

Stage 4 (ticket 05): the new ``modex_graph.GraphEngine`` constructs and
passes this context to ``Node.execute``. The wrapped ``AgentContext`` lives
in ``user_data`` and is reached via the ``agent_ctx`` property; the typed
``ReActTurnState`` is exposed directly as ``ctx.state`` (inherited from
``GraphContext``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

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

    The new ``modex_graph.GraphEngine`` calls ``node.execute(ctx: GraphContext[S])``
    — nodes receive a ``ReActGraphContext`` instance and access:
    - ``ctx.state`` — the ``ReActTurnState`` (typed).
    - ``ctx.runtime`` — the ``ReactGraphRuntime`` (AOP bridge).
    - ``ctx.agent_ctx`` — the wrapped ``AgentContext`` (framework services,
      tool_manager, history, emitter, identity, etc.).
    """

    @property
    def agent_ctx(self) -> AgentContext:
        return cast("AgentContext", self.user_data)

    @property
    def tool_manager(self) -> ToolManager | None:
        return self.agent_ctx.tool_manager

    @property
    def runtime_context_manager(self) -> RuntimeContextManager | None:
        runtime = self.agent_ctx.runtime
        return runtime.services.runtime_context_manager if runtime is not None else None


def get_agent_ctx(ctx: GraphContext[Any]) -> AgentContext:
    """Extract the ``AgentContext`` from a ``GraphContext``'s ``user_data``.

    Centralizes the ``cast(AgentContext, ctx.user_data)`` pattern that was
    repeated at 10+ call sites across nodes and runtime. The cast is
    structurally necessary because ``Node.execute`` receives ``GraphContext[S]``
    (base type), not ``ReActGraphContext`` — the ``.agent_ctx`` property on
    the subclass is unreachable without an ``isinstance`` + ``cast`` that is
    no safer than this helper.
    """
    return cast("AgentContext", ctx.user_data)


__all__ = ["ReActGraphContext", "get_agent_ctx"]
