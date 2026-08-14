"""Invocation-local execution context via ContextVar.

Each asyncio Task gets its own ``NodeExecution`` slot — concurrent tasks
never clobber each other's invocation identity. This replaces the old
shared ``ctx._current_instance`` field that raced under
``ParallelScheduler`` when concurrent tasks yielded.

Lifecycle: the scheduler sets the ContextVar before calling ``Node.run()``
and resets it in ``finally``. Token-based reset enables proper nesting
for subgraph execution (``CompiledGraph.execute``).

Per suggestion §9-13: invocation identity (instance_id, invocation) must
be truly invocation-local, not shared mutable fields on GraphContext.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .persistence.graph_metadata import InvocationContext


class NodeExecution:
    """Per-invocation execution state, task-local via ContextVar.

    Set by the scheduler (LinearScheduler / ParallelScheduler) before
    calling ``Node.run()``, reset in ``finally``. Each asyncio Task sees
    its own value — concurrent tasks don't clobber.

    Fields:
    - ``instance_id``: the currently-executing instance ID, used by
      ``ctx.dispatch()`` for source identification. Replaces the old
      shared ``ctx._current_instance``.
    - ``invocation``: the ``InvocationContext`` returned by
       ``begin_invocation``, used by ``Node._submit()`` and
       ``ctx.scratch``.
    """

    __slots__ = ("instance_id", "invocation")

    def __init__(
        self,
        instance_id: str = "",
        invocation: InvocationContext | None = None,
    ) -> None:
        self.instance_id = instance_id
        self.invocation = invocation


_current: ContextVar[NodeExecution | None] = ContextVar(
    "_modex_graph_execution", default=None
)


def get_execution() -> NodeExecution | None:
    """Get the current task's NodeExecution, or None if not in a node run."""
    return _current.get()


def set_execution(exec: NodeExecution) -> Token[NodeExecution | None]:
    """Set the current task's NodeExecution. Returns a token for reset."""
    return _current.set(exec)


def reset_execution(token: Token[NodeExecution | None]) -> None:
    """Reset the NodeExecution to its previous value (for nesting)."""
    _current.reset(token)


__all__ = [
    "NodeExecution",
    "get_execution",
    "set_execution",
    "reset_execution",
]
