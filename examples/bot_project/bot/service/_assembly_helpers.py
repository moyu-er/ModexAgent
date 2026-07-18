"""Shared pool-assembly helpers (ADR-0025, ticket 6).

Private mixin hosting the build helpers that both
:class:`ReactExecutionStrategy` and :class:`ExternalCodingExecutionStrategy`
need. The helpers moved here from ``pool_builder.py`` so that
``pool_builder.create_pool`` is strategy-agnostic (~150 lines, zero
strategy-specific branching) and the strategies no longer import from
``pool_builder`` (undoing the transitional ``import-from-pool_builder``
pattern from tickets 3/4).

Why a shared mixin (deviation from the ticket wording):
  The ticket lists the helpers as moving "into ``ReactExecutionStrategy`` as
  private methods". They do — ``ReactExecutionStrategy`` inherits this mixin,
  so the helpers ARE private methods of ``ReactExecutionStrategy`` (and of
  ``ExternalCodingExecutionStrategy``). A shared mixin avoids duplicating
  ~300 lines of build logic in the external strategy, which the ticket also
  asks to "preserve behavior" for (external_coding still builds a placeholder
  provider/terminal/tools/skill_manager until a future ticket eliminates
  that). The future elimination will simply override the relevant methods on
  ``ExternalCodingExecutionStrategy`` to return ``None`` — no other code
  change required.

The helpers are byte-for-byte the implementations that lived in
``pool_builder.py`` before ticket 6; the only change is that module-level
imports of ``detect_platform_shell`` / ``create_terminal_manager`` /
``_load_agent_mcp_tools`` / ``build_todo_store`` / ``resolve_system_prompt``
are now resolved through THIS module's namespace. Tests that previously
patched ``bot.service.pool_builder.<symbol>`` must patch
``bot.service._assembly_helpers.<symbol>`` (or the strategy class method)
instead.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bot.service.model_config import BotModelConfig, ModelCfg, ProviderCfg
from bot.service.model_provider import BotModelProvider
from modex_agent.core.tool_manager import (
    InMemoryToolManager,
    Tool,
    ToolManagerConfig,
)
from modex_agent.ioc.configs.observability import CassetteScope
from modex_agent.memory.injection import FullInjectionPolicy
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.multi_agent.pool_config import PoolAssemblyDeps
from modex_agent.multi_agent.pool_config.specs import MainAgentSpec
from modex_agent.runtime.store import JsonFileTodoStore
from modex_agent.tools.mcp.registry import McpConnectionRegistry
from modex_agent.tools.presets import (
    ToolPreset,
    get_preset_tools,
    get_supplement_tools,
)
from modex_agent.tools.terminal import SubprocessExecutor, SubprocessTool
from modex_agent.tools.terminal.backends.factory import (
    UnsupportedVisibilityForTransport,
)
from modex_agent.tools.terminal.managers import create_terminal_manager
from modex_agent.tools.terminal.types import TerminalVisibility, detect_platform_shell
from modex_agent.tools.workspace_scoped import (
    WorkspaceRootProvider,
    wrap_standard_tools,
)

if TYPE_CHECKING:
    from bot.webui.transcript_store import TranscriptStore
    from bot.workspace.handle import WorkspaceHandle, WorkspaceResolverCell
    from modex_agent.ioc.configs.app import AppConfig
    from modex_agent.pipeline.snapshot import PoolDataSnapshot

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Tiny model-config helpers (duplicated from pool_builder per ticket 6:
# "Duplicate the tiny helper" — these are 2-line functions used both by the
# strategies' build helpers AND by pool_builder's post-assembly wiring).
# ═══════════════════════════════════════════════════════════════════════════


def _placeholder_model_config() -> BotModelConfig:
    """A minimal valid BotModelConfig used when no model.yml is configured.

    Lets the bot boot so the user can configure a real model via the WebUI
    (Settings -> Models) or ``modexbot config``. The placeholder provider has
    empty api_key/base_url, so every real LLM call fails - but
    ``BotModelProvider.chat_stream`` catches the provider-build failure and
    returns an ``LLMResponse(finish_reason=ERROR)``, and the ReAct LLM/end
    nodes surface that as a turn error instead of crashing the process.
    """
    return BotModelConfig(
        default_provider="_unconfigured",
        default_model="_placeholder",
        providers=[
            ProviderCfg(
                key="_unconfigured",
                name="_unconfigured",
                api_key="",
                base_url="",
                models=[
                    ModelCfg(name="_placeholder", model="_placeholder"),
                ],
            )
        ],
    )


def _resolved_or_placeholder(cfg: BotModelConfig | None) -> BotModelConfig:
    """Return ``cfg`` when a real model is configured, else the placeholder."""
    return cfg or _placeholder_model_config()


# ═══════════════════════════════════════════════════════════════════════════
# Shared build helpers (private mixin)
# ═══════════════════════════════════════════════════════════════════════════


class _PoolAssemblyMixin:
    """Private mixin hosting the build helpers both strategies need.

    The helpers are byte-for-byte the implementations that lived in
    ``pool_builder.py`` before ticket 6. They are private (underscore-prefixed)
    instance methods so strategies call ``self._build_*(...)``.

    The mixin is NOT a strategy: it does not inherit from
    :class:`ExecutionStrategy` and is never registered. Concrete strategies
    combine it with :class:`ExecutionStrategy` via multiple inheritance.
    """

    # ── LLM provider ─────────────────────────────────────────────────────

    def _build_llm_provider(
        self, pool_name: str, bot_model_config: BotModelConfig | None
    ) -> BotModelProvider:
        provider = BotModelProvider(_resolved_or_placeholder(bot_model_config))
        logger.info("Pool '%s': BotModelProvider (default=%s)", pool_name, provider.model)
        return provider

    # ── Terminal ─────────────────────────────────────────────────────────

    def _build_terminal_manager(
        self,
        main_spec: MainAgentSpec,
        pool_name: str,
        workspace_handle: WorkspaceHandle | None,
    ) -> Any | None:
        """Create terminal manager from main agent spec.

        ADR-0010 two-axis construction. The user-facing YAML fields
        ``use_terminal`` (bool) and ``terminal_visibility`` (bool) live on the
        pool's main-agent ``MainAgentSpec``. The framework translates ``True``
        -> ``TerminalVisibility.VISIBLE`` and ``False`` ->
        ``TerminalVisibility.HIDDEN`` and constructs the manager via the
        two-axis ``create_terminal_manager(shell_info=..., visibility=...)``
        signature.

        Fallback chain: if the requested VISIBLE backend cannot be created on
        this platform (``UnsupportedVisibilityForTransport``), retry with
        HIDDEN. If HIDDEN also fails, fall back to SubprocessTool-only (return
        None) so the agent still works.
        """
        if not main_spec.use_terminal:
            logger.info("Pool '%s': use_terminal=false, skipping terminal tools", pool_name)
            return None

        visibility_bool: bool = main_spec.terminal_visibility

        shell_info = detect_platform_shell()
        if shell_info is None:
            logger.warning(
                "Pool '%s': no supported shell detected; falling back to SubprocessTool.",
                pool_name,
            )
            return None

        default_cwd: str | None = (
            str(workspace_handle.current) if workspace_handle is not None else None
        )

        attempts: list[TerminalVisibility] = (
            [TerminalVisibility.VISIBLE, TerminalVisibility.HIDDEN]
            if visibility_bool
            else [TerminalVisibility.HIDDEN]
        )

        last_err: Exception | None = None
        for vis in attempts:
            try:
                mgr = create_terminal_manager(
                    shell_info=shell_info,
                    visibility=vis,
                    default_cwd=default_cwd,
                )
                logger.info(
                    "Pool '%s': terminal manager created (family=%s, visibility=%s)",
                    pool_name,
                    shell_info.family.value,
                    vis.value,
                )
                return mgr
            except UnsupportedVisibilityForTransport as exc:
                last_err = exc
                logger.warning(
                    "Pool '%s': terminal backend (family=%s, visibility=%s) unavailable: %s",
                    pool_name,
                    shell_info.family.value,
                    vis.value,
                    exc,
                )
            except Exception as exc:
                last_err = exc
                logger.warning(
                    "Pool '%s': terminal backend (family=%s, visibility=%s) failed: %s",
                    pool_name,
                    shell_info.family.value,
                    vis.value,
                    exc,
                )

        logger.error(
            "Pool '%s': ALL terminal backends failed (tried %s). Last error: %s. "
            "Falling back to SubprocessTool only.",
            pool_name,
            attempts,
            last_err,
        )
        return None

    # ── Tools ────────────────────────────────────────────────────────────

    async def _build_tools(
        self,
        main_spec: MainAgentSpec,
        assembly_deps: PoolAssemblyDeps,
        terminal_manager: Any,
        project_dir: Path,
        output_adapter: Any,
        pool_name: str,
        data_dir: Path,
        pool_data: PoolDataSnapshot | None,
        root_provider: WorkspaceRootProvider | None,
        *,
        transcript_store: TranscriptStore | None = None,
        sessions_dir_provider: Callable[[], Path | None] | None = None,
        mcp_registry: McpConnectionRegistry | None = None,
        persistence: Any | None = None,
        app_config: Any | None = None,
    ) -> tuple[InMemoryToolManager, Any | None, JsonFileTodoStore]:
        """Build the main agent's tool manager from config.

        Tool assembly order: preset tools (file/search/bash gated by
        ``main_spec.tool_preset``), additive supplements (``main_spec.tool_supplements``,
        e.g. ast_grep), terminal tools (when ``terminal_manager`` is set), the
        custom send_file_to_user tool, the experience tool (when enabled), todo
        tools, and MCP tools resolved from ``main_spec.mcp`` via the registry.
        ``send_to_agent`` is registered separately in ``create_pool`` after the
        communication service is wired.
        """
        # Local import to keep the module-level import graph lean and to honor
        # test patches on ``bot.service._assembly_helpers._load_agent_mcp_tools``.
        from bot.service.builders import _load_agent_mcp_tools, build_todo_store

        tm = InMemoryToolManager(config=ToolManagerConfig())

        if pool_data is not None and pool_data.runtime_dir is not None:
            todo_dir: Path = pool_data.runtime_dir / "todos"
        else:
            todo_dir = data_dir / "runtime_state" / pool_name / "todos"
        from modex_agent.core.scope import RecordScope

        todo_scope = RecordScope(pool=pool_name)
        todo_store = build_todo_store(app_config, persistence, todo_dir, todo_scope)

        # Preset tools: file/search/bash gated by main_spec.tool_preset. A bash
        # factory is provided so FULL/READ_WRITE/READ_ONLY presets get a
        # workspace-scoped SubprocessTool; the terminal manager (when present)
        # registers the richer Command/Process/Terminal tools below.
        def _make_bash() -> Tool:
            sub = SubprocessTool(executor=SubprocessExecutor(), timeout=300)
            if root_provider is not None:
                wrapped = wrap_standard_tools([sub], root_provider)
                return wrapped[0]
            return sub

        preset = main_spec.tool_preset if main_spec.tool_preset is not None else ToolPreset.FULL
        for tool in get_preset_tools(
            preset, subprocess_tool_factory=_make_bash, root_provider=root_provider
        ):
            tm.register(tool)

        # Additive supplement tools (e.g. ast_grep, todo) layered on top of the preset.
        for tool in get_supplement_tools(
            main_spec.tool_supplements, root_provider=root_provider, todo_store=todo_store
        ):
            tm.register(tool)
        if main_spec.tool_supplements:
            logger.info(
                "Pool '%s': supplement tools registered: %s",
                pool_name,
                [s.value for s in main_spec.tool_supplements],
            )

        # Terminal tools - registered when a terminal manager exists (replaces the
        # preset's bash tool with the stateful Command/Process/Terminal trio).
        if terminal_manager is not None:
            from modex_agent.tools.terminal import (
                CommandTool,
                ProcessRegistry,
                ProcessTool,
                TerminalTool,
            )
            from modex_agent.tools.terminal.config import TerminalRuntimeConfig

            cfg = TerminalRuntimeConfig()
            registry = ProcessRegistry(config=cfg)
            tm.register(CommandTool(manager=terminal_manager, registry=registry, config=cfg))
            tm.register(ProcessTool(registry=registry, manager=terminal_manager))
            tm.register(TerminalTool(terminal_manager))
            logger.info(
                "Pool '%s': terminal tools registered (Command/Process/Terminal)", pool_name
            )

        # Custom tools
        from bot.tools.custom import SendFileToUserTool

        tm.register(
            SendFileToUserTool(
                output_adapter=output_adapter,
                transcript_store=transcript_store,
                media_config=assembly_deps.media,
                sessions_dir_provider=sessions_dir_provider,
            )
        )

        # Experience tool - always enabled for main agents (baked; not configurable).
        # The experience dir comes from the workspace's pool_data (fixed per
        # workspace); fallback to a data_dir relative path for non-workspace (test).
        from modex_agent.core.experience import PerFileExperienceMetaStore
        from modex_agent.memory.tools.experience import ExperienceTool

        if pool_data is not None:
            base_exp_dir: Path = pool_data.experience_dir
            _exp_path: Callable[[], Path] = lambda: base_exp_dir
        else:
            fallback = data_dir / "experiences" / pool_name / main_spec.agent_name

            def _exp_path() -> Path:
                return fallback

        _exp_path().mkdir(parents=True, exist_ok=True)
        exp_meta = PerFileExperienceMetaStore(_exp_path)
        tm.register(ExperienceTool(_exp_path, exp_meta))
        logger.info("Pool '%s': experience tool registered", pool_name)

        # MCP tools resolved from main_spec.mcp (registry names) - never let MCP
        # failures break the rest of the tool manager / pool creation.
        mcp_tools: list[Any] = []
        mcp_manager: Any | None = None
        if main_spec.mcp:
            try:
                mcp_tools, mcp_manager = await _load_agent_mcp_tools(
                    main_spec.agent_name,
                    list(main_spec.mcp),
                    project_dir,
                    mcp_registry=mcp_registry,
                )
            except Exception as exc:
                logger.warning(
                    "Pool '%s': MCP tool loading failed, skipping: %s", pool_name, exc
                )

        for tool in mcp_tools:
            tm.register(tool)
        if mcp_tools:
            logger.info("Pool '%s': %d MCP tools registered", pool_name, len(mcp_tools))

        logger.info(
            "Pool '%s': ToolManager ready (%d tools total)", pool_name, len(tm.list_tools())
        )
        return tm, mcp_manager, todo_store

    # ── Skill manager ────────────────────────────────────────────────────

    def _build_skill_manager(
        self, main_agent_name: str, project_dir: Path, pool_name: str
    ) -> Any | None:
        """Convention: skills/{pool_name}/{agent_name}/."""
        directories = [project_dir / "skills" / pool_name / main_agent_name]

        logger.info(
            "Pool '%s': scanning skills: %s (exists=%s)",
            pool_name,
            [str(d) for d in directories],
            [d.exists() for d in directories],
        )
        found = [d for d in directories if d.resolve().exists()]
        if not found:
            logger.warning("Pool '%s': no skill directories found", pool_name)
            return None

        from modex_agent.core.skills import (
            DefaultSkillBuilder,
            DirectorySkillCache,
            FileSkillSource,
            SkillManager,
        )

        source = FileSkillSource(
            directories=found, cache=True, layout="directory", skill_filename="SKILL.md",
        )
        cache = DirectorySkillCache(directories=found, layout="directory")
        builder = DefaultSkillBuilder(base_path=project_dir)
        mgr = SkillManager(source=source, builder=builder, cache=cache)
        return mgr

    # ── Cassette config ──────────────────────────────────────────────────

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

    # ── Fallback context manager ─────────────────────────────────────────

    def _fallback_context_manager(
        self, main_spec: MainAgentSpec, system_prompt: str
    ) -> Any:
        """A minimal context_manager for tests / non-workspace wiring.

        The main agent's real context manager comes from the workspace pool_data;
        this fallback keeps create_pool callable without a workspace (used by
        unit tests that mock the build steps).
        """
        return MemorySystemContextManager(
            memory_system=None,
            default_agent_id=main_spec.agent_name,
            default_agent_role="main",
            base_system_prompt=system_prompt,
            injection_policy=FullInjectionPolicy(pruned_manager=None),
            experience_manager=None,
            roles=list(main_spec.roles),
        )

    # ── Cell sessions dir ────────────────────────────────────────────────

    def _cell_sessions_dir(self, cell: WorkspaceResolverCell | None) -> Path | None:
        """Resolve the workspace sessions dir from a resolver cell.

        Returns ``None`` when the cell is not yet materialized so callers fall
        back to the ctxvar-based resolution path.
        """
        if cell is None:
            return None
        try:
            return cell.resolve_workspace().ctx.paths.sessions_dir
        except RuntimeError:
            return None
