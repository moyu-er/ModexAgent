"""Communication service and target-store builder, plus cleanup listener.

Extracted from ``pool_builder.py`` (ADR-0025 ticket 6 split). Builds the
slimmed ``AgentCommunicationService`` + target store, and provides
``UserNoticeCleanupListener`` for memory-compaction notices.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.core.scope import MemoryContext
from modex_agent.core.session_registry import SessionRegistry
from modex_agent.hook.notification import AgentNotificationService
from modex_agent.memory.cleanup_events import MemoryCleanupListener
from modex_agent.multi_agent import AgentPool
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.communication import AgentCommunicationService
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
from modex_agent.multi_agent.tools import (
    CommunicationTarget,
    CommunicationTargetStore,
)
from modex_agent.multi_agent.workspace_paths import WorkspacePathResolver

if TYPE_CHECKING:
    from modex_agent.memory.cleanup import CleanupResult
    from modex_agent.memory.core.models import CompressionReason

logger = logging.getLogger(__name__)


def _build_communication(
    pool: AgentPool,
    main_agent_name: str,
    broker: Any,
    agent_bus: Any,
    project_dir: Path,
    pool_name: str,
    templates: list,
    template_registry: AgentTemplateRegistry,
    *,
    session_registry: SessionRegistry | None = None,
    workspace_path_resolver: WorkspacePathResolver | None = None,
    trace_enabled: bool = True,
) -> tuple[AgentCommunicationService, CommunicationTargetStore]:
    """Build the slimmed AgentCommunicationService + target store.

    ADR-0015 D5: the service is a pure router - it no longer takes the ~30
    construction params it once did. ``AgentMaterializeDeps`` (built once in
    ``create_pool``) carries the subagent construction deps, injected into
    ``AgentPool`` for the Drainer-spawner. This function wires only the
    router + the target store.
    """
    main_address = AgentAddress(name=main_agent_name)
    main_service = AgentCommunicationService(
        source=main_address,
        broker=broker,
        registry=pool,
        agent_bus=agent_bus,
        template_registry=template_registry,
        pool=pool,
        pool_name=pool_name,
        project_dir=project_dir,
        session_registry=session_registry,
        workspace_path_resolver=workspace_path_resolver,
        trace_enabled=trace_enabled,
    )

    # Communication target store - populate from registered agents + templates
    main_store = CommunicationTargetStore()
    for p in pool.list_profiles():
        if p.name != main_agent_name:
            main_store.add(
                CommunicationTarget(
                    name=p.name,
                    kind=p.comm_kind,
                    description=p.role_description,
                )
            )
    for t in templates:
        main_store.add(
            CommunicationTarget(
                name=t.spec.agent_name,
                kind=AgentCommKind.SUBAGENT,
                description=t.spec.description,
                execution_strategy=t.spec.execution_strategy,
            )
        )
    logger.info("Pool '%s': communication store (%d targets)", pool_name, len(main_store.list()))
    return main_service, main_store


class UserNoticeCleanupListener(MemoryCleanupListener):
    """Pushes transient English notices when session memory is compacted.

    Fires around the blocking archive-generation LLM call so the user
    understands the pause instead of seeing a stuck agent. Notices go through
    AgentNotificationService (tagged ``message_type=notice`` so the
    ChannelRouter fans them to the originating channel AND the WebUI observer)
    and are never written to session memory/history.
    """

    _START_NOTICE = "[compact] Consolidating conversation memory, please wait..."
    _DONE_NOTICE = "[compact] Memory consolidated."

    def __init__(self, notification_service: AgentNotificationService) -> None:
        self._svc = notification_service

    async def on_cleanup_triggered(self, context: MemoryContext, reason: CompressionReason) -> None:
        session_id = context.session_id
        if session_id is None:
            return
        await self._svc.send_notice(session_id, self._START_NOTICE)

    async def on_cleanup_finished(self, context: MemoryContext, result: CleanupResult) -> None:
        # ScopedMessageHistory only calls this when result.triggered.
        session_id = context.session_id
        if session_id is None:
            return
        await self._svc.send_notice(session_id, self._DONE_NOTICE)
