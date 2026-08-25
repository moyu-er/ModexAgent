"""Root-result capture for the harbor pool entry.

Turn completion for the entry is the SessionTreeManager's tree quiesce —
one lifecycle, one convergence mechanism (AGENTS.md Convergence Rule 3):
``pool_mode.execute_pool_entry`` delivers the instruction through
``tree.deliver(..., track_consume=True)`` and awaits
``tree.wait_quiesce(tree_id)``. The tree's DISPATCHED tracks, running set,
and pending-input set cover the root turn and any in-flight subagents
uniformly — a lost emitter emission can no longer hang the entry (the
tb21-all-v6 failure: completed turns, then a 17–22 minute silent hang until
the harbor wall-clock SIGKILL lost every artifact).

This module contributes NO synchronization of its own: it only captures the
root session's terminal ``AgentResult`` so the entry can write artifacts
once the tree has quiesced. When the terminal emission itself was lost,
:func:`read_back_root_result` recovers the final assistant content from the
root session history through the same memory subsystem every reader uses
(``get_full_history`` — the ContextForkBuilder precedent); the entry then
needs BOTH independent subsystems to fail before its artifacts go empty.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from modex_agent.agents.react.agent import ReActEvent
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult, ContentEmitter
from modex_agent.core.scope import MemoryContext
from modex_agent.core.types import MessageRole

if TYPE_CHECKING:
    from modex_agent.memory.core.system import MemorySystem

__all__ = [
    "RootResultCapture",
    "RootResultCaptureEmitter",
    "read_back_root_result",
]

logger = logging.getLogger(__name__)

_READ_BACK_MESSAGE_LIMIT: int = 50


class RootResultCapture:
    """Latest terminal ``AgentResult`` of the root session — no waiting.

    The last root emission before tree quiesce is the entry's final answer:
    an error mid-run is overwritten by a later recovery, and a recovery by a
    later error. Emissions from other sessions are ignored — a subagent's
    answer reaches the root through the tree/poller and drives a later root
    turn, whose own emission then wins.
    """

    def __init__(self, root_session_id: str) -> None:
        self._root_session_id = root_session_id
        self._result: AgentResult | None = None

    def record(self, session_id: str, result: AgentResult) -> None:
        """Record a terminal emission; only the root session's is kept."""
        if session_id == self._root_session_id:
            self._result = result

    @property
    def result(self) -> AgentResult | None:
        """Root result at quiesce time; ``None`` when no emission arrived."""
        return self._result


class RootResultCaptureEmitter(ContentEmitter[ReActEvent]):
    """Session-aware terminal emitter routing every event into the capture."""

    def __init__(self, capture: RootResultCapture, session_id: str) -> None:
        super().__init__()
        self._capture = capture
        self._session_id = session_id

    def wants_streaming(self) -> bool:
        # The emitter discards deltas, but streaming activates chunk-level
        # DispatchDeadline renewal — without it a healthy max-effort LLM call
        # slower than dispatch_timeout dies like a hung one (tb21-full-bm1).
        return True

    async def emit_delta(self, delta: str) -> None:
        _ = delta

    async def emit_complete(self, result: AgentResult) -> None:
        self._capture.record(self._session_id, result)

    async def emit_error(self, error: str) -> None:
        self._capture.record(self._session_id, AgentResult(error=error))


async def read_back_root_result(
    memory_system: MemorySystem,
    root_session_id: str,
) -> AgentResult | None:
    """Recover the root result from session history when the emission was lost.

    The tree quiesced, so the root turn provably ended; if the capture is
    still empty the terminal emission was dropped. The turn's final
    assistant content lives in the root session's persisted history — read
    it back through ``get_full_history`` (the same seam ContextForkBuilder
    uses) and surface it as a COMPLETED result. ``None`` when history is
    empty or unreadable: the caller treats that as before.
    """
    try:
        context = MemoryContext(session_id=root_session_id)
        history = await memory_system.get_full_history(
            context, limit=_READ_BACK_MESSAGE_LIMIT
        )
    except Exception:
        logger.exception(
            "Root-result read-back failed for session %s", root_session_id
        )
        return None
    for message in reversed(history):
        if (
            message.role == MessageRole.ASSISTANT
            and isinstance(message.content, str)
            and message.content.strip()
        ):
            return AgentResult(content=message.content, stop_reason=StopReason.COMPLETED)
    return None
