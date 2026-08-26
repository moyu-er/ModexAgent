"""Main-agent pipeline wiring.

Extracted from ``pool_builder.py`` (ADR-0025 ticket 6 split). Wires hooks,
interceptors, governance, and command processor on the main-agent pipeline.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
    from modex_agent.multi_agent.session_tree.session_binding import (
        SessionBindingStore,
    )
    from modex_graph.context import GraphContext

from bot.service.model_choice import ModelChoiceBindHook, ModelChoiceRegistry
from bot.service.model_config import BotModelConfig
from modex_agent.agents.external.cli_resolver import resolve_modexctl_bin_dir
from modex_agent.agents.external.types import ExternalEnvSpec
from modex_agent.core.capabilities import ModelInfo
from modex_agent.core.tool_manager import ToolManager
from modex_agent.hook import HookErrorPolicy, HookSpec
from modex_agent.hook.builtin import NativeEnvInjectionHook
from modex_agent.hook.notification import TurnOutcomeNotifyHook
from modex_agent.hook.wiring import register_tree_aware_hooks
from modex_agent.ioc.factories.governance import create_governance
from modex_agent.multi_agent import AgentPool
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.communication.peer_resolution import PeerLink
from modex_agent.multi_agent.pool_config import PoolAssemblyDeps
from modex_agent.pipeline.turn_context_config import wire_graph_turn_config
from modex_agent.runtime.store import TodoStore
from modex_agent.scope.spec import AgentSpec, PoolSpec
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider
from modex_agent.trace.cassette import (
    CassetteFlushHook,
    CassetteRecorder,
)

from ..external_strategy import (
    _build_agent_pool_map,
    _build_targets,
)
from ..model_config import _resolved_or_placeholder

logger = logging.getLogger(__name__)


def _add_hook(pipeline: Any, hook: Any) -> None:
    if pipeline.hook_runner is not None:
        pipeline.hook_runner.add(HookSpec(hook=hook, on_error=HookErrorPolicy.LOG))
    else:
        pipeline.hooks.append(hook)


def _wire_main_pipeline(
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
    bot_model_config: BotModelConfig | None,
    model_choice_registry: ModelChoiceRegistry,
    roster_hook_names: frozenset[str],
    cassette_recorder: CassetteRecorder | None = None,
    control_origin: str = "",
    graph_context_resolver: Callable[[int], GraphContext[Any] | None] | None = None,
    session_binding_store: SessionBindingStore | None = None,
    tree_manager: SessionTreeManager | None = None,
    component_hook_specs: tuple[HookSpec, ...] = (),
    todo_store: TodoStore | None = None,
) -> None:
    """Wire hooks, interceptors, governance, and command processor on main pipeline.

    The experience review hook and turn_store are NOT wired here - the review
    hook is dispatched from the roster's ``+experience_review`` HOOK-slot
    declaration (bot.yml), resolved by the FW hook factory at Stage 4 assembly,
    and turn_store is resolved per turn from the workspace snapshot.

    ``root_provider`` is the per-workspace working-dir provider (the SAME one
    the file tools use). It anchors the approval classifier's ``./*`` patterns
    to the active workspace so in-workspace writes are auto-allowed; without it
    the classifier would fall back to ``project_dir`` (the bot project), gating
    every in-workspace write as DANGEROUS.

    ``roster_hook_names`` is the pool roster's final hook list
    (``assembly_spec.hooks``). A code-wired default whose roster name is in
    the set was already dispatched onto the pipeline's hook_runner by Stage 4
    assembly — wiring it again would double-register it (the roster reference
    wins, the same name-based dedup as the core's ``extra_hooks``).
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
    # Tree-aware per-pool hooks — converge with subagent path via
    # register_tree_aware_hooks (also called by AgentTemplate.materialize).
    if tree_manager is not None:
        register_tree_aware_hooks(
            pipeline.hook_runner,
            tree_manager,
            roster_hook_names=roster_hook_names,
            todo_store=todo_store,
        )
    # Roster name of ModelChoiceBindHook's factory (plugins/bot_hooks.py) —
    # Stage 4 already dispatched it onto this hook_runner.
    if "model_choice_bind" not in roster_hook_names:
        _add_hook(
            pipeline,
            ModelChoiceBindHook(
                _resolved_or_placeholder(bot_model_config),
                model_choice_registry,
            ),
        )
    if cassette_recorder is not None:
        _add_hook(pipeline, CassetteFlushHook(cassette_recorder))

    # NativeEnvInjectionHook — populate _modex_env / _current_session_id
    # contextvars at BEFORE_GRAPH so native agent subprocess tools receive
    # MODEX_* env vars. Only the main-agent pipeline reaches here; the
    # external branch in create_pool skips _wire_main_pipeline.
    # The template's session_id / agent_name are placeholders overridden
    # per-turn from ctx.session inside the hook.
    #
    # pool_map/targets are shared with external_strategy via
    # _build_agent_pool_map / _build_targets (same business layer, same
    # declared-pool source) so a peer-read bug fix lands in one place.
    agent_pool_map = _build_agent_pool_map(pool_name, pool_spec, peer_links)
    targets = _build_targets(pool_spec, peer_links)

    env_spec_template = ExternalEnvSpec(
        workspace_root=project_dir,
        inbox_root=project_dir / ".modex" / "inbox",
        workdir=project_dir,
        session_id=f"__pending__.{root_agent_name}",
        agent_name=root_agent_name,
        provider_session_id="",
        agent_pool_map=agent_pool_map,
        targets=targets,
        modexctl_bin_dir=resolve_modexctl_bin_dir(),
        comm_kind=AgentCommKind.NORMAL,
        control_origin=control_origin,
    )
    # Roster name of NativeEnvInjectionHook's factory (FW defaults/hooks.py)
    # — Stage 4 already dispatched it onto this hook_runner.
    if "native_env" not in roster_hook_names:
        _add_hook(pipeline, NativeEnvInjectionHook(env_spec_template=env_spec_template))

    # ExternalTurnRunner has no builder/approval_renderer, so access them
    # through the ABC's typed read-only properties (None for external).
    turn_runner = pipeline._turn_runner
    builder = turn_runner.turn_context_builder
    approval = turn_runner.approval_renderer
    if builder is not None:
        builder.interceptor_chain = shared_interceptor_chain
        builder.governance = create_governance(assembly_deps.memory)
    if approval is not None:
        approval.user_interface = im_ui

    from modex_agent.ioc.factories.approval import build_approval_runtime
    from modex_agent.runtime.services import AgentRuntimeServices

    approval_runtime = build_approval_runtime(
        main_spec.approval, project_root=project_dir, root_provider=root_provider
    )
    resolved_cfg = _resolved_or_placeholder(bot_model_config)
    default_resolved = resolved_cfg.default_resolved()

    services_kwargs: dict[str, Any] = {
        "safety": pipeline.safety,
        "model_info": ModelInfo(
            model_name=default_resolved.model.model,
            capabilities=default_resolved.capabilities,
        ),
    }
    if approval_runtime is not None:
        services_kwargs["approval"] = approval_runtime
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
        "Pool '%s': pipeline wired - cmd_processor=%s, skill_manager=%s",
        pool_name,
        type(pipeline.command_processor).__name__,
        type(pipeline.skill_manager).__name__ if pipeline.skill_manager else None,
    )
