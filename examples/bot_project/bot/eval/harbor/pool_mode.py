from __future__ import annotations

import logging
import uuid
from pathlib import Path

from plugins.bot_strategies import BotDefaultLLMConfig

from bot.eval.harbor.entry import EntryConfig
from bot.eval.harbor.pool_budget import pool_budget_config_from_env, register_pool_budget
from bot.eval.harbor.pool_mode_artifacts import (
    PoolTraceStore,
    write_pool_artifacts,
    write_trace_record,
)
from bot.eval.harbor.pool_mode_assembly import (
    _load_eval_app_config as _load_eval_app_config,
)
from bot.eval.harbor.pool_mode_assembly import build_eval_pool_assembly
from bot.eval.harbor.pool_mode_convergence import (
    RootResultCapture,
    RootResultCaptureEmitter,
    read_back_root_result,
)
from bot.eval.harbor.pool_mode_types import (
    PoolModeConfig,
    PoolModeDependencies,
    PoolTaskResultArtifact,
    PoolUsageArtifact,
    build_delegation_metrics,
    build_model_config,
)
from bot.eval.probes.budget import BudgetLedger
from bot.service.model_choice import ModelChoiceRegistry
from bot.service.model_provider import BotModelProvider
from bot.service.pool.factory import create_pool
from bot.service.session_pool_index import SessionPoolIndex
from bot.workspace.handle import WorkspaceHandle
from modex_agent.core.emitter import AgentResult
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.session_id import SessionInfo
from modex_agent.messaging.broker import AddressKind
from modex_agent.messaging.broker_bridge import BrokerInputPayload
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.plugins.abc import ComponentSlot, SimpleFactory
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import ComponentRegistryLoader, PluginDiscoveryConfig
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.tools.terminal.persistent_bash import PersistentBashTool

logger = logging.getLogger(__name__)

__all__ = [
    "PoolModeConfig",
    "PoolModeDependencies",
    "PoolTaskResultArtifact",
    "execute_pool_entry",
]


async def _registry(
    config: PoolModeConfig,
    dependencies: PoolModeDependencies,
) -> tuple[ComponentRegistry, BudgetLedger]:
    # Same discovery shape as the production entry (pool/factory.py):
    # project plugins must load so the declaration's roster-referenced
    # HOOK-slot components (e.g. ``+user_notice_cleanup`` from
    # plugins/bot_hooks.py) resolve at Stage 4.
    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(),),
            project_plugin_paths=(config.project_dir / "plugins",),
        ),
    )
    provider_factory = dependencies.provider_factory or SimpleFactory(
        BotModelProvider(build_model_config(config.entry)),
        BotDefaultLLMConfig,
    )
    registry.register(
        ComponentSlot.LLM_PROVIDER,
        "bot_default",
        provider_factory,
        overwrite=True,
    )
    ledger = register_pool_budget(
        registry,
        dependencies.pricebook,
        pool_budget_config_from_env(config.budget_environment),
    )
    return registry, ledger


def _root_session_id(entry: EntryConfig, agent_name: str) -> str:
    """``harbor_<task>_<item>.<agent>`` — inner segments joined with ``_``.

    The prefix must stay a single ``.``-free segment: ``agent_of`` parses the
    agent name as the SECOND dot-separated component (``split('.', 2)[1]``)
    while ``SessionInfo.from_str`` uses ``rpartition`` — a multi-dot prefix
    (``harbor.<task>.<item>.<agent>``) makes those two parsers disagree and
    breaks ``agent_of`` consumers (e.g. memory cleanup grouping). The task
    segment drops when unnamed.
    """
    if entry.task_name:
        return f"harbor_{entry.task_name}_{entry.experiment.item_id}.{agent_name}"
    return f"harbor_{entry.experiment.item_id}.{agent_name}"


def _root_input_envelope(
    root_session: SessionInfo,
    instruction: str,
    *,
    trace_id: str,
    workspace: Path,
) -> AgentMessageEnvelope:
    """Build the root turn's ``external_input`` envelope.

    Carries the same ``BrokerInputPayload`` shape ``AgentPool.submit_input``
    writes, so ``dispatch_envelope``'s ``to_input_message`` reconstruction
    preserves the full InputMessage semantics (content, trace_id metadata,
    workspace). It is delivered through ``SessionTreeManager.deliver`` with
    ``track_consume=True`` — the graph-node pattern
    (``bot/graph/agent_node.py``): the DISPATCHED MessageTrack anchors tree
    quiesce from delivery until the poller's ``on_dispatch_end``, so the
    entry's wait can neither race past the turn nor return on an empty tree.
    """
    payload = BrokerInputPayload(
        content=instruction,
        session_id=root_session.session_id_prefix,
        agent_session_id=root_session.session_id,
        metadata={"trace_id": trace_id},
        workspace=str(workspace),
        message_type=AgentMessageType.EXTERNAL_INPUT,  # extra field, allowed by extra="allow"
    )
    return AgentMessageEnvelope(
        payload=payload.model_dump(exclude_none=True),
        source=AgentAddress(kind=AddressKind.CHANNEL, name="user"),
        target=AgentAddress(kind=AddressKind.AGENT, name=root_session.agent_name),
        message_type=AgentMessageType.EXTERNAL_INPUT,
        session_id=root_session.session_id_prefix,
        agent_session_id=root_session.session_id,
    )


async def execute_pool_entry(
    config: PoolModeConfig,
    dependencies: PoolModeDependencies | None = None,
) -> PoolTaskResultArtifact:
    deps = dependencies or PoolModeDependencies()
    instruction = config.entry.instruction_path.read_text(encoding="utf-8")
    trace_id = uuid.uuid4().hex
    trace_store = PoolTraceStore(config, deps.span_exporter)
    registry, ledger = await _registry(config, deps)
    broker = InMemoryMessageBroker()
    await broker.start()
    child_sessions: list[str] = []
    session_turn_counts: dict[str, int] = {}

    def emitter_factory(session_id: str, pool_name: str) -> RootResultCaptureEmitter:
        _ = pool_name
        # The framework creates one emitter per agent turn, so this counts
        # turns for the delegation metrics in usage.json.
        session_turn_counts[session_id] = session_turn_counts.get(session_id, 0) + 1
        return RootResultCaptureEmitter(capture, session_id)

    async def on_subagent_created(child_id: str, parent_id: str, pool_name: str) -> None:
        _ = parent_id, pool_name
        child_sessions.append(child_id)

    assembly = await build_eval_pool_assembly(
        config,
        trace_store=trace_store,
        broker=broker,
        component_registry=registry,
    )
    pool_instance = None
    agent_result: AgentResult | None = None
    failure: str | None = None
    # The try starts before the first post-assembly I/O (the trace record
    # write) so a raise there still runs the teardown below — the broker
    # and the assembly's persistence handle must not leak.
    try:
        root_session = SessionInfo.from_str(
            _root_session_id(config.entry, assembly.declared.pool.root_agent.name)
        )
        capture = RootResultCapture(root_session.session_id)
        write_trace_record(config, trace_id, root_session.session_id)
        pool_instance = await create_pool(
            pool_name=config.pool_name,
            declared=assembly.declared,
            assembly_deps=assembly.assembly_deps,
            project_dir=config.project_dir,
            data_dir=config.data_dir,
            broker=broker,
            output_adapter=assembly.output_adapter,
            safety=RuntimeSafetyPolicy(),
            retention=assembly.retention,
            im_ui=assembly.output_adapter,
            shared_hooks=assembly.shared_hooks,
            shared_hook_runner=assembly.shared_hook_runner,
            shared_interceptor_chain=assembly.shared_interceptor_chain,
            control_channel=None,
            command_processor=None,
            pool_data=assembly.pool_data,
            workspace_handle=WorkspaceHandle(
                target=config.entry.task_workspace, data_root=config.data_dir
            ),
            workspace_resolver=assembly.resolver_cell,
            emitter_factory=emitter_factory,
            on_subagent_created=on_subagent_created,
            session_registry=assembly.session_registry,
            session_store=assembly.session_store,
            transcript_store=None,
            bot_model_config=assembly.bot_model_config,
            model_choice_registry=ModelChoiceRegistry(),
            mcp_registry=None,
            persistence=assembly.persistence,
            app_config=assembly.app_config,
            kb_provider=None,
            strategy_registry=None,
            session_pool_index=SessionPoolIndex(),
            workspace_registry=None,
            workspace_resources=assembly.resources,
            component_registry=registry,
        )
        assembly.resolver_cell.set(assembly.resources)
        # One lifecycle, one convergence mechanism (AGENTS.md Rule 3): the
        # instruction is delivered THROUGH the session tree — deliver's
        # tracked DISPATCHED record anchors quiesce from delivery until the
        # poller's on_dispatch_end — then the entry waits for tree quiesce
        # (root turn + any in-flight subagents). No local timeout: a hang
        # here would be a signal-source bug to fix, not to fence.
        tree = pool_instance.tree_manager
        await tree.deliver(
            root_session.session_id,
            _root_input_envelope(
                root_session,
                instruction,
                trace_id=trace_id,
                workspace=config.entry.task_workspace,
            ),
            track_consume=True,
        )
        tree_id = await tree.tree_id_for_session(root_session.session_id)
        if tree_id is not None:
            await tree.wait_quiesce(tree_id)
        agent_result = capture.result
        if agent_result is None:
            # The tree quiesced but the capture is empty: the terminal
            # emission was lost. The turn's final assistant content is
            # persisted in the root session history — read it back before
            # teardown closes the memory system (tb21-all-v6: a lost
            # emission used to write empty result/usage artifacts).
            memory_system = assembly.pool_data.context_manager.memory_system
            if memory_system is not None:
                agent_result = await read_back_root_result(
                    memory_system, root_session.session_id
                )
                if agent_result is not None:
                    logger.warning(
                        "Root terminal emission was lost; recovered result from "
                        "session history (trace %s)",
                        trace_id,
                    )
    finally:
        if pool_instance is not None:
            await pool_instance.pool.shutdown_all()
            fallback_bash = pool_instance.tool_manager.get_tool("bash")
            if isinstance(fallback_bash, PersistentBashTool):
                await fallback_bash.close()
        await broker.stop()
        memory_system = assembly.pool_data.context_manager.memory_system
        if memory_system is not None:
            await memory_system.close()
        # Last, mirroring workspace teardown ordering: WAL-checkpoint and
        # close the job's state.db so it is inspectable after the trial.
        if assembly.persistence is not None:
            await assembly.persistence.close()
    if agent_result is not None and agent_result.error is not None:
        failure = agent_result.error
    elif agent_result is not None and str(agent_result.stop_reason).lower() in {
        "cancelled",
        "error",
    }:
        failure = (
            f"turn ended with stop_reason={agent_result.stop_reason} and no error detail; "
            "likely a dispatch watchdog termination (e.g. hung LLM call) — see trace "
            f"{trace_id} in Langfuse"
        )
    trace_store.close()
    spent_usd = ledger.spent_cost_usd
    outcome = PoolTaskResultArtifact(
        trace_id=trace_id,
        output=agent_result.content or "" if agent_result is not None else "",
        stop_reason=str(agent_result.stop_reason) if agent_result is not None else None,
        error=failure,
        dropped_span_count=trace_store.dropped_span_count,
        memory_namespace=config.entry.memory_namespace,
        pool_name=config.pool_name,
        spent_usd=spent_usd,
        child_sessions=tuple(child_sessions),
    )
    write_pool_artifacts(
        config,
        instruction,
        outcome,
        PoolUsageArtifact(
            **trace_store.usage.model_dump(),
            spent_usd=spent_usd,
            delegation=build_delegation_metrics(
                root_session.session_id, child_sessions, session_turn_counts
            ),
        ),
    )
    return outcome
