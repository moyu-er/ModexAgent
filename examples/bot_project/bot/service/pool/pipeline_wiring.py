"""Main-agent pipeline wiring.

Extracted from ``pool_builder.py`` (ADR-0025 ticket 6 split). Wires hooks,
interceptors, governance, and command processor on the main-agent pipeline.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from bot.service.model_choice import ModelChoiceBindHook, ModelChoiceRegistry
from bot.service.model_config import BotModelConfig
from modex_agent.agents.external.cli_resolver import resolve_modexctl_bin_dir
from modex_agent.agents.external.types import ExternalEnvSpec
from modex_agent.core.capabilities import ModelInfo
from modex_agent.core.tool_manager import ToolManager
from modex_agent.hook import HookErrorPolicy, HookSpec
from modex_agent.hook.builtin import NativeEnvInjectionHook
from modex_agent.hook.notification import TurnOutcomeNotifyHook
from modex_agent.ioc.factories.governance import create_governance
from modex_agent.multi_agent import AgentPool
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.pool_config import PoolAssemblyDeps
from modex_agent.multi_agent.pool_config.specs import MainAgentSpec, PoolSpec
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider
from modex_agent.trace.cassette import (
    CassetteFlushHook,
    CassetteRecorder,
)

from .._assembly_helpers import _resolved_or_placeholder
from ..external_strategy import (
    _build_agent_pool_map,
    _build_targets,
)

logger = logging.getLogger(__name__)


def _add_hook(pipeline: Any, hook: Any) -> None:
    if pipeline.hook_runner is not None:
        pipeline.hook_runner.add(HookSpec(hook=hook, on_error=HookErrorPolicy.LOG))
    else:
        pipeline.hooks.append(hook)


def _wire_main_pipeline(
    pool: AgentPool,
    main_agent_name: str,
    inbox_consumer: Any,
    notification_service: Any,
    shared_interceptor_chain: Any,
    im_ui: Any,
    main_spec: MainAgentSpec,
    assembly_deps: PoolAssemblyDeps,
    project_dir: Path,
    command_processor: Any,
    pool_name: str,
    tool_manager: ToolManager,
    pool_spec: PoolSpec,
    *,
    root_provider: WorkspaceRootProvider | None = None,
    bot_model_config: BotModelConfig | None,
    model_choice_registry: ModelChoiceRegistry,
    cassette_recorder: CassetteRecorder | None = None,
    control_origin: str = "",
) -> None:
    """Wire hooks, interceptors, governance, and command processor on main pipeline.

    The experience review hook and turn_store are NOT wired here - the review
    hook is built in bot.workspace.wiring.pool_wiring._wire_pool_to_resources from
    the workspace's pool_data, and turn_store is resolved per turn from the
    workspace snapshot.

    ``root_provider`` is the per-workspace working-dir provider (the SAME one
    the file tools use). It anchors the approval classifier's ``./*`` patterns
    to the active workspace so in-workspace writes are auto-allowed; without it
    the classifier would fall back to ``project_dir`` (the bot project), gating
    every in-workspace write as DANGEROUS.
    """
    main_instance = pool._agents.get(main_agent_name)
    if main_instance is None or main_instance.pipeline is None:
        logger.warning(
            "Pool '%s': cannot wire pipeline - main_instance=%s",
            pool_name,
            type(main_instance).__name__ if main_instance else None,
        )
        return

    pipeline = main_instance.pipeline

    # Hooks
    # InboxFlushHook is NOT added here: the AgentFactory auto-injects it onto
    # pipeline.hook_runner for every agent (main + subagent) with
    # inbox_strategy != "none", so fold-in is wired in one place.
    _add_hook(pipeline, TurnOutcomeNotifyHook(notification_service=notification_service))
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
    # contextvars at BEFORE_TURN so native agent subprocess tools receive
    # MODEX_* env vars. Only the main-agent pipeline reaches here; the
    # external branch in create_pool skips _wire_main_pipeline.
    # The template's session_id / agent_name are placeholders overridden
    # per-turn from ctx.session inside the hook.
    #
    # pool_map/targets are shared with external_strategy via
    # _build_agent_pool_map / _build_targets (same business layer, same
    # PoolSpec source) so a peer-read bug fix lands in one place.
    agent_pool_map = _build_agent_pool_map(pool_name, pool_spec, project_dir)
    targets = _build_targets(pool_name, pool_spec, project_dir)

    env_spec_template = ExternalEnvSpec(
        workspace_root=project_dir,
        inbox_root=project_dir / ".modex" / "inbox",
        workdir=project_dir,
        session_id=f"__pending__.{main_agent_name}",
        agent_name=main_agent_name,
        provider_session_id="",
        agent_pool_map=agent_pool_map,
        targets=targets,
        modexctl_bin_dir=resolve_modexctl_bin_dir(),
        comm_kind=AgentCommKind.NORMAL,
        control_origin=control_origin,
    )
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
