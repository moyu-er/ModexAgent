"""ReactExecutionStrategy - assembles react pools (ADR-0025, ticket 3).

Transitional home: lives in ``examples/bot_project/bot/service/`` (NOT in
``src/modex_agent/agents/react/``) because ``assemble()`` calls the bot-side
``_build_*`` helpers (now in :mod:`bot.service._assembly_helpers`) which use
bot-layer types (``BotModelProvider``, ``BotModelConfig``, ``WorkspaceHandle``).
A future ticket may relocate this class to
``src/modex_agent/agents/react/strategy.py`` once the bot-layer dependencies
are abstracted away.

The strategy is stateless: ``assemble()`` is called once per pool at build
time and returns a :class:`StrategyAssembly` whose react-only fields are
populated. ``agent`` and ``turn_runner`` are ``None`` - the ``Agent``
instance + ``ReActTurnRunner`` are created downstream by the factory +
pipeline.

Ticket 6: the ``_build_*`` helpers moved from ``pool_builder.py`` into the
shared :class:`_PoolAssemblyMixin` (in :mod:`bot.service._assembly_helpers`).
``ReactExecutionStrategy`` inherits the mixin, so the helpers are private
methods of this class (``self._build_llm_provider``, etc.). The
``import-from-pool_builder`` pattern from ticket 3 is undone.
"""

from __future__ import annotations

import logging
from pathlib import Path

from modex_agent.multi_agent.execution_strategy import (
    ExecutionStrategy,
    PoolAssemblyContext,
    StrategyAssembly,
)
from modex_agent.multi_agent.pool_config.specs import PoolSpec
from modex_agent.trace.cassette import apply_cassette_wrapping

from ._assembly_helpers import _PoolAssemblyMixin

logger = logging.getLogger(__name__)


class ReactExecutionStrategy(_PoolAssemblyMixin, ExecutionStrategy):
    """Assemble react pools (graph-driven ReAct loop, the framework default).

    Inherits the shared ``_build_*`` helpers from :class:`_PoolAssemblyMixin`
    so ``assemble()`` can construct provider/terminal/tools/skill/etc. without
    importing from ``pool_builder``.
    """

    @property
    def name(self) -> str:
        return "react"

    @property
    def supports_subagents(self) -> bool:
        return True

    @property
    def requires_main_agent_tools(self) -> bool:
        return True

    def validate_pool_spec(self, spec: PoolSpec) -> None:
        """No-op for react - react accepts any valid PoolSpec."""
        return None

    async def assemble(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
        """Build react-only components and return them in a StrategyAssembly.

        Calls the shared ``_build_*`` helpers (inherited from
        :class:`_PoolAssemblyMixin`) with the same arguments the inline code
        in ``pool_builder.create_pool`` previously passed, in the same order.
        The resulting objects are the same instances the inline code would
        have produced.
        """
        from bot.service.builders import resolve_system_prompt

        pool_name = ctx.pool_name
        pool_spec = ctx.pool_spec
        main_spec = pool_spec.main
        main_agent_name = main_spec.agent_name
        project_dir: Path = ctx.project_dir
        data_dir: Path = ctx.data_dir
        workspace_handle = ctx.workspace_handle
        workspace_resolver = ctx.workspace_resolver
        bot_model_config = ctx.bot_model_config
        app_config = ctx.app_config
        pool_data = ctx.pool_data
        assembly_deps = ctx.assembly_deps
        if assembly_deps is None:
            raise RuntimeError(
                "ReactExecutionStrategy.assemble: ctx.assembly_deps must be set "
                "by pool_builder.create_pool before calling strategy.assemble()."
            )

        system_prompt = resolve_system_prompt(main_agent_name, project_dir)

        provider = self._build_llm_provider(pool_name, bot_model_config)
        terminal_manager = self._build_terminal_manager(main_spec, pool_name, workspace_handle)

        if pool_data is not None:
            context_manager = pool_data.context_manager
        else:
            context_manager = self._fallback_context_manager(main_spec, system_prompt)

        root_provider = None
        if workspace_handle is not None:
            from bot.workspace.handle import WorkspaceHandleRootProvider

            root_provider = WorkspaceHandleRootProvider(workspace_handle)

        def sessions_dir_provider() -> Path | None:
            if workspace_resolver is not None:
                return self._cell_sessions_dir(workspace_resolver)
            return None

        tool_manager, mcp_manager, todo_store = await self._build_tools(
            main_spec,
            assembly_deps,
            terminal_manager,
            project_dir,
            ctx.output_adapter,
            pool_name,
            data_dir,
            pool_data,
            root_provider,
            transcript_store=ctx.transcript_store,
            sessions_dir_provider=sessions_dir_provider,
            mcp_registry=ctx.mcp_registry,
            persistence=ctx.persistence,
            app_config=app_config,
        )

        cassette_enabled, cassette_scope, cassette_base_dir = self._resolve_cassette_config(
            app_config, data_dir
        )
        provider, tool_manager, cassette_recorder = apply_cassette_wrapping(
            provider,
            tool_manager,
            cassette_enabled=cassette_enabled,
            cassette_scope=cassette_scope,
            base_dir=cassette_base_dir,
        )

        skill_manager = self._build_skill_manager(main_agent_name, project_dir, pool_name)

        return StrategyAssembly(
            agent=None,
            turn_runner=None,
            provider=provider,
            tool_manager=tool_manager,
            skill_manager=skill_manager,
            mcp_manager=mcp_manager,
            terminal_manager=terminal_manager,
            context_manager=context_manager,
            notification_service=None,
            communication_service=None,
            target_store=None,
            cassette_recorder=cassette_recorder,
            todo_store=todo_store,
            root_provider=root_provider,
            extra_cleanup=(),
        )
