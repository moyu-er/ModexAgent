"""PA-03 — the ``session_title`` hook (OutcomeFinallyHook + ClosableHook).

Fires on FINALLY_GRAPH after each actual_turn. Only COMPLETED user main
sessions are named; the hook's sole job is to submit a background naming
task (never awaited here — the main reply must not wait for the model).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from modex_agent.core.emitter import AgentResult, StopReason
from modex_agent.hook.abc import ClosableHook, OutcomeFinallyHook

if TYPE_CHECKING:
    from bot.service.session_title_task import SessionTitleNamingTask
    from modex_agent.core.agent import AgentContext

logger = logging.getLogger(__name__)


class SessionTitleHook(OutcomeFinallyHook, ClosableHook):
    """Name a completed user main session in the background."""

    def __init__(self, naming: SessionTitleNamingTask, pool: str) -> None:
        self._naming = naming
        self._pool = pool

    @property
    def name(self) -> str:
        return "session_title"

    async def on_outcome(self, ctx: AgentContext, result: AgentResult) -> None:
        """Submit the naming task for COMPLETED user main sessions only."""
        if result.stop_reason is not StopReason.COMPLETED:
            return
        session = ctx.session
        # Root check: subagent sessions (recorded parent) never auto-name.
        if session.parent_session_id is not None:
            return
        # Root identity alone is not proof of user input; the naming task
        # itself skips when no real user content exists in the transcript.
        self._naming.submit(self._pool, session)

    async def aclose(self) -> None:
        """Pool-stop recovery: cancel THIS pool's pending naming tasks only.

        The naming owner and its lazy provider are workspace-shared —
        other pools' tasks keep running and the provider stays open until
        workspace teardown (DESIGN §2.4; PLAN §3.1). Idempotent.
        """
        await self._naming.close_pool(self._pool)

__all__ = ["SessionTitleHook"]
