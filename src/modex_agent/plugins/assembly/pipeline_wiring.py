"""Main-agent pipeline wiring.

Promoted from ``examples/bot_project/bot/service/pool/pipeline_wiring.py``
(W4a, plan SD-7). Wires hooks, interceptors, governance, and the command
processor on the main-agent pipeline. The deployment model universe is not
read here — the caller passes the resolved ``model_info`` value object.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.core.capabilities import ModelInfo
from modex_agent.core.workspace_root import WorkspaceRootProvider
from modex_agent.hook import HookErrorPolicy, HookSpec
from modex_agent.hook.notification import TurnOutcomeNotifyHook
from modex_agent.memory.assembly import create_governance
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.multi_agent import AgentPool
from modex_agent.multi_agent.communication.peer_resolution import PeerLink
from modex_agent.multi_agent.pool_config import PoolAssemblyDeps
from modex_agent.pipeline.turn_context_config import wire_graph_turn_config
from modex_agent.scope.spec import AgentSpec, PoolSpec
from modex_agent.trace.cassette import (
    CassetteFlushHook,
    CassetteRecorder,
)

if TYPE_CHECKING:
    from modex_agent.core.tool_manager import ToolManager
    from modex_agent.core.turn.approval_decision import ApprovalAuditStore
    from modex_agent.hook.abc import HookSpec
    from modex_agent.multi_agent.session_tree.session_binding import (
        SessionBindingStore,
    )
    from modex_agent.sandbox.settings import SandboxSettings
    from modex_graph.context import GraphContext

logger = logging.getLogger(__name__)

__all__ = ["wire_main_pipeline"]


def _add_hook(pipeline: Any, hook: Any) -> None:
    if pipeline.hook_runner is not None:
        pipeline.hook_runner.add(HookSpec(hook=hook, on_error=HookErrorPolicy.LOG))
    else:
        pipeline.hooks.append(hook)


def _declared_sandbox_settings(main_spec: AgentSpec) -> SandboxSettings | None:
    """The root's declared ``sandbox_guard`` sandbox section, if any.

    Reads the SAME ``interceptor_configs["sandbox_guard"]`` declaration the
    interceptor factory consumes — one declaration, two assemblies (the
    guard interceptor at execution time, the composite approval classifier
    at classification time) sharing the identical settings. A missing or
    DEFAULT-tier section returns None: the approval assembly stays the
    plain tiered classifier (unified-security Ticket 02 double-gate).
    """
    from modex_agent.sandbox.settings import SandboxBackend, SandboxSettings

    raw = (main_spec.interceptor_configs or {}).get("sandbox_guard")
    if raw is None:
        return None
    section = raw.get("sandbox", {}) if isinstance(raw, dict) else {}
    settings = SandboxSettings.model_validate(section)
    return None if settings.backend is SandboxBackend.DEFAULT else settings


def wire_main_pipeline(
    pool: AgentPool,
    root_agent_name: str,
    inbox_consumer: Any,
    notification_service: Any,
    shared_interceptor_chain: Any,
    im_ui: Any,
    main_spec: AgentSpec,
    assembly_deps: PoolAssemblyDeps,
    project_dir: Path,
    command_processor: Any,
    pool_name: str,
    tool_manager: ToolManager,
    pool_spec: PoolSpec,
    peer_links: tuple[PeerLink, ...] = (),
    *,
    root_provider: WorkspaceRootProvider | None = None,
    model_info: ModelInfo | None = None,
    cassette_recorder: CassetteRecorder | None = None,
    graph_context_resolver: Callable[[int], GraphContext[Any] | None] | None = None,
    session_binding_store: SessionBindingStore | None = None,
    component_hook_specs: tuple[HookSpec, ...] = (),
    approval_audit_store: ApprovalAuditStore | None = None,
    memory_context_manager: MemorySystemContextManager | None = None,
) -> None:
    """Wire interceptors, governance, and command processor on the main pipeline.

    No hook is unconditionally injected here anymore (the W6 glue
    eradication): ``deliver_retry`` / ``length_guard`` / ``native_env`` are
    compiler position-default roster entries dispatched by Stage 4, and
    ``model_choice_bind`` is a declared roster entry dispatched by the same
    path — every factory derives its construction deps from the context
    chain. What remains code-wired: the two outcome hooks below
    (TurnOutcomeNotify, CassetteFlush) — deployment-level
    notification/recording glue, not declaration-face components.

    The experience review hook and turn_store are NOT wired here - the review
    hook is dispatched from the roster's ``+experience_review`` HOOK-slot
    declaration, resolved by the FW hook factory at Stage 4 assembly, and
    turn_store is resolved per turn from the workspace snapshot.

    ``root_provider`` is the per-workspace working-dir provider (the SAME one
    the file tools use). It anchors the approval classifier's ``./*`` patterns
    to the active workspace so in-workspace writes are auto-allowed; without it
    the classifier would fall back to ``project_dir`` (the deployment root),
    gating every in-workspace write as DANGEROUS.
    """
    main_instance = pool._agents.get(root_agent_name)
    if main_instance is None or main_instance.pipeline is None:
        logger.warning(
            "Pool '%s': cannot wire pipeline - main_instance=%s",
            pool_name,
            type(main_instance).__name__ if main_instance else None,
        )
        return

    pipeline = main_instance.pipeline

    if component_hook_specs:
        pipeline.hook_runner.extend(list(component_hook_specs))

    # Hooks
    # InboxFlushHook is NOT added here: the AgentFactory auto-injects it onto
    # pipeline.hook_runner for every agent (main + subagent) with
    # inbox_strategy != "none", so fold-in is wired in one place.
    _add_hook(pipeline, TurnOutcomeNotifyHook(notification_service=notification_service))
    if cassette_recorder is not None:
        _add_hook(pipeline, CassetteFlushHook(cassette_recorder))

    # ExternalTurnRunner has no builder/approval_renderer, so access them
    # through the ABC's typed read-only properties (None for external).
    turn_runner = pipeline._turn_runner
    builder = turn_runner.turn_context_builder
    approval = turn_runner.approval_renderer
    if builder is not None:
        builder.interceptor_chain = shared_interceptor_chain
        # Memory-backed pools get the persistent-compaction governance head.
        # The resolver goes through the SAME context manager load() uses, so
        # compaction addresses the session's real MemoryContext (T3 hard
        # constraint); memory_system None (test fallback CM) keeps the old
        # tool-chain-only chain.
        memory_system = (
            memory_context_manager.memory_system
            if memory_context_manager is not None
            else None
        )
        memory_context_resolver = (
            (
                lambda actx: memory_context_manager.resolve_memory_context(
                    actx.session.session_id
                )
            )
            if memory_system is not None
            else None
        )
        builder.governance = create_governance(
            assembly_deps.memory,
            memory_system=memory_system,
            memory_context_resolver=memory_context_resolver,
        )
    if approval is not None:
        approval.user_interface = im_ui

    from modex_agent.plugins.defaults.capabilities.approval.factory import (
        build_approval_runtime,
    )
    from modex_agent.runtime.services import AgentRuntimeServices

    sandbox_settings = _declared_sandbox_settings(main_spec)
    approval_runtime = build_approval_runtime(
        main_spec.approval,
        project_root=project_dir,
        root_provider=root_provider,
        sandbox=sandbox_settings,
    )

    services_kwargs: dict[str, Any] = {
        "safety": pipeline.safety,
        "model_info": model_info,
    }
    if approval_runtime is not None:
        services_kwargs["tool_gate"] = approval_runtime
        if approval_audit_store is not None:
            # Guard decisions ride the unified audit timeline (Ticket 06);
            # the sink is wired only when the guard composite exists, so
            # plain deployments write nothing extra.
            services_kwargs["approval_audit"] = approval_audit_store
    if sandbox_settings is not None:
        # Graph turns swap tool_gate for the escalate-off guard composite
        # (guard_only_gate; unified-security Ticket 05b: never set None —
        # the guard HARDLINE verdicts must survive the graph's arbitration
        # shutdown). Built from the SAME declared settings/root provider
        # as the composite above — one declaration, one decision service
        # shape.
        from modex_agent.plugins.defaults.capabilities.approval.security import (
            guard_only_runtime,
        )
        from modex_agent.sandbox.decision import SecurityDecisionService

        if root_provider is None:
            # build_approval_runtime already raised for a sandbox tier
            # without a root provider — reaching here means that contract
            # was bypassed.
            raise RuntimeError(
                "wire_main_pipeline: a declared sandbox_guard section "
                "requires root_provider (the guard composite anchors its "
                "HARDLINE verdicts to the active workspace root)"
            )
        services_kwargs["guard_only_gate"] = guard_only_runtime(
            decision=SecurityDecisionService(
                settings=sandbox_settings, workspace_root_provider=root_provider
            )
        )
    if builder is not None:
        builder.runtime_services = AgentRuntimeServices(**services_kwargs)

    # Graph-context resolver + turn-context config pipeline (6 configurators).
    # The resolver is a lazy closure that defers workspace resolution +
    # orchestrator dereference to invocation time (F6-verified pattern).
    # Ticket 12: converged on the shared ``wire_graph_turn_config`` — the
    # same trio ``AgentTemplate.materialize`` wires onto lazy subagents.
    if builder is not None:
        wire_graph_turn_config(
            builder,
            graph_context_resolver=graph_context_resolver,
            session_binding_store=session_binding_store,
        )

    # Command processor (convention: use provided, else default)
    if command_processor is not None:
        pipeline.command_processor = command_processor
    else:
        from modex_agent.commands.processor import SlashCommandProcessor

        pipeline.command_processor = SlashCommandProcessor.default()

    logger.info(
        "Pool '%s': pipeline wired - cmd_processor=%s, skill_resolver=%s",
        pool_name,
        type(pipeline.command_processor).__name__,
        type(pipeline.skill_resolver).__name__ if pipeline.skill_resolver else None,
    )
