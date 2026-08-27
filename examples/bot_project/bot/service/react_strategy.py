"""ReactExecutionStrategy - assembles react pools (ADR-0025, ticket 3).

Transitional home: lives in ``examples/bot_project/bot/service/`` (NOT in
``src/modex_agent/agents/react/``) because ``assemble()`` calls the bot-side
``_build_*`` helpers (in :mod:`bot.service.builders`) which use
bot-layer types (``BotModelConfig``, ``WorkspaceHandle``).
A future ticket may relocate this class to
``src/modex_agent/agents/react/strategy.py`` once the bot-layer dependencies
are abstracted away.

The strategy is stateless: ``assemble()`` is called once per pool at build
time and returns a :class:`StrategyAssembly` whose react-only fields are
populated. ``agent`` and ``turn_runner`` are ``None`` - the ``Agent``
instance + ``ReActTurnRunner`` are created downstream by the factory +
pipeline.

The LLM provider is NOT assembled here: the LLM_PROVIDER slot resolves
name→instance once in ``create_pool`` (``bot/service/pool/factory.py``) and
feeds both the agent factory and the Stage-4 assembly inputs (C1).

The ``_build_*`` helpers live on the shared :class:`_PoolAssemblyMixin`
(in :mod:`bot.service.builders`). ``ReactExecutionStrategy`` inherits the
mixin, so the helpers are private methods of this class
(``self._build_tools``, etc.). The terminal manager ladder itself is
framework logic (:func:`create_terminal_manager_or_none`); the strategy
only supplies the pool's config axes and workspace cwd.
"""

from __future__ import annotations

from pathlib import Path

from modex_agent.core.tool_manager import ToolManager
from modex_agent.multi_agent.execution_strategy import (
    ExecutionStrategy,
    PoolAssemblyContext,
    StrategyAssembly,
)
from modex_agent.scope.spec import PoolSpec
from modex_agent.tools.terminal.managers import create_terminal_manager_or_none
from modex_agent.trace.cassette import CassetteRecorder

from .builders import _PoolAssemblyMixin, build_pool_todo_store


class ReactExecutionStrategy(_PoolAssemblyMixin, ExecutionStrategy):
    """Assemble react pools (graph-driven ReAct loop, the framework default).

    Inherits the shared ``_build_*`` helpers from :class:`_PoolAssemblyMixin`
    so ``assemble_main()`` can construct terminal/tools/skill/etc. without
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

    def validate_pool_spec(self, pool: PoolSpec) -> None:
        """No-op for react — react accepts any declared pool."""
        return None

    async def assemble_main(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
        """Build react-only components and return them in a StrategyAssembly.

        Calls the shared ``_build_*`` helpers (inherited from
        :class:`_PoolAssemblyMixin`) with the same arguments the inline code
        in ``pool_builder.create_pool`` previously passed, in the same order.
        The resulting objects are the same instances the inline code would
        have produced.
        """
        pool_name = ctx.pool_name
        pool_spec = ctx.pool_spec
        main_spec = pool_spec.root_agent
        root_agent_name = main_spec.name
        project_dir: Path = ctx.project_dir
        data_dir: Path = ctx.data_dir
        workspace_handle = ctx.workspace_handle
        app_config = ctx.app_config
        pool_data = ctx.pool_data

        terminal_manager = create_terminal_manager_or_none(
            use_terminal=main_spec.use_terminal,
            terminal_visibility=main_spec.terminal_visibility,
            pool_name=pool_name,
            default_cwd=(
                str(workspace_handle.current) if workspace_handle is not None else None
            ),
        )
        # Pool todo store — supplied infra: handed to the roster's
        # TodoToolFactory via pool_runtime.todo_store (Stage 3 harvests it
        # from the StrategyAssembly below).
        todo_store = build_pool_todo_store(
            app_config, ctx.persistence, pool_data, pool_name, data_dir
        )
        prompt_provider = None

        # create_pool unconditionally injects assembly_spec + component_registry
        # (factory.py), so the system prompt always comes from the
        # SYSTEM_PROMPT_PROVIDER slot resolved in native_core.
        system_prompt = ""

        if pool_data is not None:
            context_manager = pool_data.context_manager
        else:
            context_manager = self._fallback_context_manager(main_spec, system_prompt)

        root_provider = None
        if workspace_handle is not None:
            from bot.workspace.handle import WorkspaceHandleRootProvider

            root_provider = WorkspaceHandleRootProvider(workspace_handle)

        tool_manager: ToolManager = await self._build_tools(
            pool_name,
            kb_provider=ctx.kb_provider,
        )
        # The bash slot is NOT built or registered here: the roster owns it.
        # Stage 4 resolves the compiled ``bash`` entry through the FW
        # BashToolFactory (CommandTool with a terminal manager / the pool's
        # PersistentBashTool fallback / SubprocessTool on no-pty hosts), and
        # native_core ensures the bash_input companion after roster
        # registration — the same single road the subagent template uses.

        # Cassette recording wraps the strategy's own products (tool manager);
        # the provider (resolved in create_pool) is wrapped with the same
        # recorder by build_native_inputs.
        cassette_enabled, cassette_scope, cassette_base_dir = self._resolve_cassette_config(
            app_config, data_dir
        )
        cassette_recorder: CassetteRecorder | None = None
        if cassette_enabled:
            cassette_recorder = CassetteRecorder(cassette_base_dir, scope=cassette_scope)
            tool_manager = cassette_recorder.wrap_tool_executor(tool_manager)

        skill_manager = self._build_skill_manager(root_agent_name, project_dir, pool_name)

        return StrategyAssembly(
            agent=None,
            turn_runner=None,
            system_prompt_provider=prompt_provider,
            tool_manager=tool_manager,
            skill_manager=skill_manager,
            mcp_manager=None,
            terminal_manager=terminal_manager,
            persistent_bash=None,
            context_manager=context_manager,
            notification_service=None,
            communication_service=None,
            target_store=None,
            cassette_recorder=cassette_recorder,
            todo_store=todo_store,
            root_provider=root_provider,
            component_hook_specs=(),
            extra_cleanup=(),
        )
