"""Cleanup notice hook for memory compaction.

``AgentCommunicationService`` + the per-agent ``CommunicationTargetStore``
are built inline by ``pool/factory.py`` from the compiled declaration
(declared children for the root store; peer targets via
``resolve_peer_targets`` in Phase 2) — the legacy template-scan builder
retired with the roster road.
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

    Registered via ``memory_system.add_cleanup_hook(...)`` (the converged
    memory hook runner), NOT on the ReAct ``HookRunner``.
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
