"""Cleanup notice hook for memory compaction.

The communication service + the per-agent ``CommunicationTargetStore``
are built by the framework's ``subagents`` capability (the pool supply
carries the service; the per-agent stores ride the capability's wiring
artifacts) — this module owns only the cleanup-notice hook.
"""

from __future__ import annotations

from modex_agent.hook.notification import AgentNotificationService
from modex_agent.memory.hooks import (
    CleanupFinishedHook,
    CleanupTriggeredHook,
    MemoryHookContext,
)


class UserNoticeCleanupHook(CleanupTriggeredHook, CleanupFinishedHook):
    """Pushes transient English notices when session memory is compacted.

    One instance implements both memory hook points so the user sees a
    paired start/done notice around the blocking archive-generation LLM
    call. Notices go through ``AgentNotificationService`` (tagged
    ``message_type=notice`` so the ChannelRouter fans them to the
    originating channel AND the WebUI observer) and are never written to
    session memory/history.

    Registered via the memory hook runner (``add_cleanup_hook`` on the
    pool's memory system — the roster dispatch path for the declared
    ``+user_notice_cleanup`` entry), NOT on the ReAct ``HookRunner``.
    """

    _START_NOTICE = "[compact] Consolidating conversation memory, please wait..."
    _DONE_NOTICE = "[compact] Memory consolidated."

    def __init__(self, notification_service: AgentNotificationService) -> None:
        self._svc = notification_service

    async def on_cleanup_triggered(self, ctx: MemoryHookContext) -> None:
        memory_context = ctx.memory_context
        if memory_context is None:
            return
        session_id = memory_context.session_id
        if session_id is None:
            return
        await self._svc.send_notice(session_id, self._START_NOTICE)

    async def on_cleanup_finished(self, ctx: MemoryHookContext) -> None:
        # ScopedMessageHistory only dispatches FINISHED when result.triggered.
        memory_context = ctx.memory_context
        if memory_context is None:
            return
        session_id = memory_context.session_id
        if session_id is None:
            return
        await self._svc.send_notice(session_id, self._DONE_NOTICE)
