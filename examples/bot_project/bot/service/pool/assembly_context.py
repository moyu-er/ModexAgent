"""Assembly context builder and fallback context manager.

Extracted from ``pool_builder.py`` (ADR-0025 ticket 6 split). Builds the
frozen :class:`PoolAssemblyContext` passed to ``strategy.assemble`` and
provides a minimal fallback context manager for tests / non-workspace wiring.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bot.service.model_choice import ModelChoiceRegistry
from bot.service.model_config import BotModelConfig
from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.core.emitter import ContentEmitter
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.session_registry import SessionRegistry
from modex_agent.core.session_store import SessionStore
from modex_agent.hook import HookRunner
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext
from modex_agent.multi_agent.pool_config import PoolAssemblyDeps
from modex_agent.multi_agent.pool_config.specs import MainAgentSpec, PoolSpec
from modex_agent.pipeline.adapters import OutputAdapter
from modex_agent.pipeline.snapshot import PoolDataSnapshot

if TYPE_CHECKING:
    from bot.webui.transcript_store import TranscriptStore
    from bot.workspace.handle import (
        WorkspaceHandle,
        WorkspaceResolverCell,
    )
    from modex_agent.tools.mcp.registry import McpConnectionRegistry


def _build_assembly_context(
    *,
    pool_name: str,
    pool_spec: PoolSpec,
    project_dir: Path,
    data_dir: Path,
    broker: Any,
    inbox_server: Any,
    agent_bus: Any,
    output_adapter: OutputAdapter,
    safety: RuntimeSafetyPolicy,
    retention: SessionRetentionPolicy,
    workspace_handle: WorkspaceHandle | None,
    workspace_resolver: WorkspaceResolverCell | None,
    emitter_factory: Callable[[str], ContentEmitter] | None,
    app_config: Any | None,
    persistence: Any | None,
    mcp_registry: McpConnectionRegistry | None,
    shared_hooks: list,
    shared_hook_runner: HookRunner,
    shared_interceptor_chain: Any,
    session_registry: SessionRegistry | None,
    session_store: SessionStore | None,
    bot_model_config: BotModelConfig | None,
    model_choice_registry: ModelChoiceRegistry,
    command_processor: Any | None,
    control_channel: InMemoryControlChannel | None,
    pool_data: PoolDataSnapshot | None,
    transcript_store: TranscriptStore | None,
    assembly_deps: PoolAssemblyDeps,
) -> PoolAssemblyContext:
    """Build the frozen :class:`PoolAssemblyContext` passed to ``strategy.assemble``."""
    return PoolAssemblyContext(
        pool_name=pool_name,
        pool_spec=pool_spec,
        project_dir=project_dir,
        data_dir=data_dir,
        broker=broker,
        inbox_server=inbox_server,
        agent_bus=agent_bus,
        output_adapter=output_adapter,
        safety=safety,
        retention=retention,
        registry=None,  # type: ignore[arg-type]
        workspace_handle=workspace_handle,
        workspace_resolver=workspace_resolver,
        emitter_factory=emitter_factory,
        app_config=app_config,
        persistence=persistence,
        mcp_registry=mcp_registry,
        shared_hooks=shared_hooks,
        shared_hook_runner=shared_hook_runner,
        shared_interceptor_chain=shared_interceptor_chain,
        session_registry=session_registry,
        session_store=session_store,
        bot_model_config=bot_model_config,
        model_choice_registry=model_choice_registry,
        command_processor=command_processor,
        control_channel=control_channel,
        pool_data=pool_data,
        transcript_store=transcript_store,
        on_session_start=None,
        on_session_end=None,
        router=None,
        assembly_deps=assembly_deps,
    )


def _fallback_context_manager(main_spec: MainAgentSpec, system_prompt: str) -> Any:
    """A minimal context_manager for tests / non-workspace wiring.

    The main agent's real context manager comes from the workspace pool_data;
    this fallback keeps create_pool callable without a workspace (used by
    unit tests that mock the build steps).

    Duplicated from :mod:`bot.service._assembly_helpers` (ticket 6: "Duplicate
    the tiny helper") because ``create_pool`` needs it for the
    provider-unavailable path (when the strategy did not produce a context
    manager) and we do not want ``create_pool`` to import from the strategy
    module.
    """

    from modex_agent.memory.injection import FullInjectionPolicy
    from modex_agent.memory.system import MemorySystemContextManager

    return MemorySystemContextManager(
        memory_system=None,
        default_agent_id=main_spec.agent_name,
        default_agent_role="main",
        base_system_prompt=system_prompt,
        injection_policy=FullInjectionPolicy(pruned_manager=None),
        experience_manager=None,
        roles=list(main_spec.roles),
    )
