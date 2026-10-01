"""ReactExecutionStrategy — assembles react pools (ADR-0025, ticket 3).

Promoted from ``examples/bot_project/bot/service/react_strategy.py`` (W4a,
plan SD-7): the relocation TODO in that module's docstring is closed here.
The strategy is stateless: ``assemble_main()`` is called once per pool at
build time and returns a :class:`StrategyAssembly` whose react products
are populated. The main runtime (``ReActAgent`` + ``ReActTurnRunner``) is
constructed downstream by Stage 4 through the native core's react
runtime constructor (the default ``DefaultAgentFactory``).

The LLM provider is NOT assembled here: the LLM_PROVIDER slot resolves
name→instance once in ``create_pool``
(:mod:`modex_agent.plugins.assembly.pool_factory`) and feeds both the agent
factory and the Stage-4 assembly inputs (C1).

The former ``_PoolAssemblyMixin`` helpers (bot ``service/builders.py``) are
private methods of this class. Shell construction is capability-owned and
does not pass through the strategy result.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.core.tool_manager import ToolManager
from modex_agent.memory.injection import FullInjectionPolicy
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.multi_agent.execution_strategy import (
    ExecutionStrategy,
    ReactMainProducts,
    StrategyAssembly,
)
from modex_agent.scope.runtime_ownership import BUNDLED_REACT_OWNERSHIP, RuntimeOwnership
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.trace.cassette import CassetteRecorder
from modex_agent.trace.observability import CassetteScope
from modex_agent.workspace.handle import (
    StaticRootProvider,
    WorkspaceHandleRootProvider,
)

if TYPE_CHECKING:
    from modex_agent.app.config import AppConfig
    from modex_agent.core.workspace_root import WorkspaceRootProvider
    from modex_agent.memory.context import ContextManager
    from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext
    from modex_agent.scope.spec import AgentSpec

logger = logging.getLogger(__name__)

__all__ = ["ReactExecutionStrategy"]


class ReactExecutionStrategy(ExecutionStrategy):
    """Assemble react pools (graph-driven ReAct loop, the framework default)."""

    @property
    def name(self) -> str:
        return "react"

    @property
    def ownership(self) -> RuntimeOwnership:
        """React: the framework owns every component face (LLM provider,
        main-agent tool surface, memory, approval, subagents); the loop
        does not own context assembly (the memory system builds it).
        Returns the scope-owned bundled constant — the single
        declaration both the strategy and the compile-time fallback
        read."""
        return BUNDLED_REACT_OWNERSHIP

    async def assemble_main(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
        """Build react-only components and return them in a StrategyAssembly.

        Constructs the same objects the inline code in the historical
        ``pool_builder.create_pool`` produced, in the same order.
        """
        pool_name = ctx.pool_name
        pool_spec = ctx.pool_spec
        main_spec = pool_spec.root_agent
        data_dir: Path = ctx.data_dir
        workspace_handle = ctx.workspace_handle
        app_config = ctx.app_config
        pool_data = ctx.pool_data

        # The pool's todo store is NOT built here: the ``todo`` capability's
        # supply() owns its construction (Stage 3 aggregation →
        # capability_supply['todo'] → the roster's TodoToolFactory).

        # create_pool unconditionally injects assembly_spec + component_registry,
        # so the system prompt always comes from the SYSTEM_PROMPT_PROVIDER
        # slot resolved in native_core.
        system_prompt = ""

        context_manager: ContextManager | None
        if pool_data is not None:
            context_manager = pool_data.context_manager
        else:
            context_manager = self._fallback_context_manager(main_spec, system_prompt)

        # The sandbox guard factory requires a root on EVERY pool boot
        # (fail-fast), so the provider is always produced: the live
        # workspace root when a workspace exists, the project dir otherwise
        # (workspace-less wiring).
        root_provider: WorkspaceRootProvider
        if workspace_handle is not None:
            root_provider = WorkspaceHandleRootProvider(workspace_handle)
        else:
            root_provider = StaticRootProvider(ctx.project_dir)

        tool_manager: ToolManager = await self._build_tools(pool_name)
        # The shell capability contributes the ``bash`` anchor. Stage 4's
        # single TOOL factory returns and owns the complete effective group.

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

        # The pool's skill resolver is NOT built here: the skills capability's
        # supply owns catalog construction and Stage 4's ``build_native_inputs``
        # looks the root resolver up from the aggregated ``capability_supply`` —
        # after Stage 3 aggregates it.

        return StrategyAssembly(
            tool_manager=tool_manager,
            context_manager=context_manager,
            root_provider=root_provider,
            react_products=ReactMainProducts(cassette_recorder=cassette_recorder),
        )

    # ── Private build helpers (former bot ``_PoolAssemblyMixin``) ────────

    async def _build_tools(self, pool_name: str) -> InMemoryToolManager:
        """Build the main agent's base tool manager (empty).

        Every tool — preset tools, supplements (bash/edit/aci/todo/
        experience), the communication entries, the terminal trio, the
        opt-in business tools, and per-agent MCP tools — is registered by
        Stage 4 through the TOOL-slot factories / the FW MCP loader reading
        the context chain, on top of the empty base manager this builder
        returns.
        """
        tm = InMemoryToolManager()
        logger.info(
            "Pool '%s': ToolManager ready (%d tools total)", pool_name, len(tm.list_tools())
        )
        return tm

    def _resolve_cassette_config(
        self, app_config: AppConfig | None, data_dir: Path
    ) -> tuple[bool, CassetteScope, Path]:
        base_dir = data_dir / "cassette"
        if app_config is None or app_config.observability is None:
            return False, CassetteScope.DEFAULT, base_dir
        return (
            app_config.observability.cassette_enabled,
            app_config.observability.cassette_scope,
            base_dir,
        )

    def _fallback_context_manager(
        self, main_spec: AgentSpec, system_prompt: str
    ) -> MemorySystemContextManager:
        """A minimal context_manager for tests / non-workspace wiring.

        The main agent's real context manager comes from the workspace pool_data;
        this fallback keeps create_pool callable without a workspace (used by
        unit tests that mock the build steps).
        """
        return MemorySystemContextManager(
            # Test/non-workspace seam: the real memory system comes from
            # pool_data; the declared type assumes a live DefaultMemorySystem.
            memory_system=None,  # type: ignore[arg-type]
            default_agent_id=main_spec.name,
            default_agent_role="main",
            base_system_prompt=system_prompt,
            injection_policy=FullInjectionPolicy(),
            roles=list(main_spec.roles),
        )
