"""Communication service and target-store builder, plus cleanup notice hook.

Extracted from ``pool_builder.py`` (ADR-0025 ticket 6 split). Builds the
slimmed ``AgentCommunicationService`` + target store, and provides
``UserNoticeCleanupHook`` for memory-compaction notices.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.session_registry import SessionRegistry
from modex_agent.hook.notification import AgentNotificationService
from modex_agent.memory.hooks import (
    CleanupFinishedHook,
    CleanupTriggeredHook,
    MemoryHookContext,
)
from modex_agent.multi_agent import AgentPool
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.communication import AgentCommunicationService
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
from modex_agent.multi_agent.tools import (
    CommunicationTarget,
    CommunicationTargetStore,
    SendToPeerTool,
    TaskDispatchTool,
)
from modex_agent.multi_agent.workspace_paths import WorkspacePathResolver

if TYPE_CHECKING:
    from modex_agent.multi_agent.session_tree.manager import SessionTreeManager

logger = logging.getLogger(__name__)


def _build_communication(
    pool: AgentPool,
    main_agent_name: str,
    project_dir: Path,
    pool_name: str,
    templates: list,
    template_registry: AgentTemplateRegistry,
    *,
    tree: SessionTreeManager,
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
        registry=pool,
        tree=tree,
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


def register_communication_tools(instance: PoolInstance) -> None:
    """Register a main agent's communication tools based on available targets.

    Single convergence point for both LLM-facing communication tools: ``task``
    (subagent dispatch) and ``send_to_peer`` (peer communication). Called once
    per pool after Phase 2 peer wiring, so both subagent and peer targets are
    present in the store and the presence checks reflect reality. External main
    agents have no tool surface (they communicate via ``modexctl send``), so
    they are skipped.
    """
    if instance.main_execution_strategy == ExecutionStrategyKind.EXTERNAL:
        return

    store = instance.target_store
    source = AgentAddress(name=instance.main_agent_name)
    service = instance.communication_service

    if store.list_subagents():
        instance.tool_manager.register(
            TaskDispatchTool(store=store, source=source, service=service)
        )
        logger.info("Pool '%s': task tool registered (subagent dispatch)", instance.name)
    else:
        logger.info("Pool '%s': no subagents — task tool not registered", instance.name)

    if store.list_peers():
        instance.tool_manager.register(
            SendToPeerTool(store=store, source=source, service=service)
        )
        logger.info("Pool '%s': send_to_peer tool registered (peer communication)", instance.name)
    else:
        logger.info("Pool '%s': no peers — send_to_peer tool not registered", instance.name)


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
