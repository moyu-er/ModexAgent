"""Agent build helpers — tool registration, memory, descriptors, runtime builders.

Hosts the ``AgentBuilderMixin`` for BotService, the shared
``_PoolAssemblyMixin`` for the execution strategies, tool/MCP builders,
and the runtime builders (hook runner / control channel / command
processor).

All tool registration methods use Tool objects directly (code-passed).
No tool configuration is read from YAML/config dicts.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bot.scope import BotRecordScope
from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.core.prompt import SystemPromptProvider
from modex_agent.core.scope import RecordScope
from modex_agent.core.session_store import SessionStore
from modex_agent.core.skills import SkillManager
from modex_agent.core.tool_manager import (
    InMemoryToolManager,
    Tool,
    ToolManagerConfig,
)
from modex_agent.hook.abc import Hook
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.ioc.configs.observability import CassetteScope
from modex_agent.memory.injection import FullInjectionPolicy
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import AgentMessageBus
from modex_agent.multi_agent.pool_router import PoolRoutingStore
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.pipeline.adapters import OutputAdapter
from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.assembly.context import AgentContext as ComponentAgentContext
from modex_agent.scope.spec import AgentSpec
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.registry import ScopeRegistryStore

if TYPE_CHECKING:
    from bot.kb.provider import KbProvider
    from bot.service.pool.declaration import DeclaredPoolBuild
    from bot.workspace.handle import WorkspaceResolverCell
    from modex_agent.commands.processor import SlashCommandProcessor
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.hook.runner import HookRunner
    from modex_agent.memory.registry import MemoryStoreRegistry
    from modex_agent.persistence.managers import (
        RegistryPersistenceManager,
        WorkspacePersistenceManager,
    )
    from modex_agent.plugins.registry import ComponentRegistry
    from modex_agent.runtime.codec import RuntimeStateCodecRegistry

logger = logging.getLogger(__name__)


async def resolve_declared_root_prompt(
    declared: DeclaredPoolBuild,
    project_dir: Path,
    registry: ComponentRegistry,
) -> str:
    """Resolve the compiled root's declared system-prompt provider."""
    spec = declared.root.spec
    prompt_config = dict(spec.system_prompt_config)
    if "path" in prompt_config:
        prompt_path = Path(prompt_config["path"])
        if not prompt_path.is_absolute():
            prompt_config["path"] = str(project_dir / prompt_path)
    workspace = WorkspaceContext(
        target=project_dir,
        paths=WorkspacePaths(root=project_dir / ".modex"),
        is_home=False,
    )
    factory = registry.resolve(ComponentSlot.SYSTEM_PROMPT_PROVIDER, spec.system_prompt_provider)
    config = factory.config_model.model_validate(prompt_config)
    provider: SystemPromptProvider = await factory.create(
        config,
        ComponentAgentContext(
            registry=registry,
            workspace_ctx=workspace,
            agent_name=spec.agent_name,
            spec=spec,
        ),
    )
    return await provider.get_or_refresh()


# ── Standard tool builders (code objects, no config) ──


def _make_file_tools() -> list[Tool]:
    from modex_agent.tools.standard import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool

    return [ReadFileTool(), WriteFileTool(), EditFileTool(), ListDirTool()]


def _make_search_tools() -> list[Tool]:
    from modex_agent.tools.standard import GlobTool, SearchFilesTool

    return [SearchFilesTool(), GlobTool()]


# ── MCP tool helpers ──


class AgentBuilderMixin:
    """Mixin providing shared fields used by pool-mode BotService.

    All fields below are provided by the host ``BotService`` class.
    They are declared here so the mixin's contract is visible to type checkers and IDEs.
    """

    # ── Fields provided by the host BotService class ──

    # Configuration
    _app_config: AppConfig | None

    # Core components
    output_adapter: OutputAdapter
    broker: InMemoryMessageBroker | None
    agent_bus: AgentMessageBus | None

    # Subagent caches
    _subagent_skill_managers: dict[str, SkillManager]
    _subagent_memory_systems: dict[str, Any]
    _additional_subagent_memory_systems: dict[str, Any]

    _transcript_store: Any | None = None

    # ── Properties provided by BotService ──

    @property
    def _project_dir(self) -> Path:
        """Project root directory. Implemented by BotService."""
        raise NotImplementedError


# ── Pool assembly helpers (shared by both execution strategies) ──


def _make_task_id_provider() -> Callable[[], str | None]:
    """从 env 拿 taskId。图调度时 env 已注入 (graphInstanceId)。
    非 graph 场景 env 无 MODEX_TASK_ID → None = 无 task 隔离。"""

    def _provider() -> str | None:
        return os.environ.get("MODEX_TASK_ID")

    return _provider


def _make_session_id_provider() -> Callable[[], str | None]:
    """从 env 拿 sessionId。有值 = 按 session 隔离; 无值 = 无 session 隔离。"""

    def _provider() -> str | None:
        return os.environ.get("MODEX_SESSION_ID")

    return _provider


class _PoolAssemblyMixin:
    """Private mixin hosting the build helpers both strategies need.

    The helpers are byte-for-byte the implementations that lived in
    ``pool_builder.py`` before ADR-0025 ticket 6. They are private
    (underscore-prefixed) instance methods so strategies call
    ``self._build_*(...)``.

    The mixin is NOT a strategy: it does not inherit from
    :class:`ExecutionStrategy` and is never registered. Concrete strategies
    combine it with :class:`ExecutionStrategy` via multiple inheritance.
    """

    # ── Tools ────────────────────────────────────────────────────────────

    async def _build_tools(
        self,
        pool_name: str,
        *,
        kb_provider: KbProvider | None = None,
        register_kb_tool: bool = False,
    ) -> InMemoryToolManager:
        """Build the main agent's base tool manager.

        Tool assembly is fully roster-driven (scope-assembly ticket 05 +
        ticket 10): every tool — preset tools, supplements (bash/edit/aci/
        todo/experience), the communication entries, the terminal trio,
        and per-agent MCP tools — is registered by Stage 4 through the
        TOOL-slot factories / the FW MCP loader reading the context chain,
        on top of the empty base manager this builder returns. The only
        builder-registered tool is the opt-in KB tool.
        """
        tm = InMemoryToolManager(config=ToolManagerConfig())

        # KB tool — KbProvider is built but KbTool is NOT registered to any
        # agent yet.  The tool implementation, CLI command, and REST route are
        # fully functional; agents access KB via `modexctl kb` (external) or
        # the REST endpoint directly.  To enable in-process agent usage, set
        # register_kb_tool=True (or remove the guard) once the feature has
        # been validated in production.
        if kb_provider is not None and register_kb_tool:
            from bot.tools.kb import KbTool

            task_id_provider = _make_task_id_provider()
            session_id_provider = _make_session_id_provider()
            tm.register(KbTool(kb_provider, task_id_provider, session_id_provider))
            logger.info("Pool '%s': kb tool registered", pool_name)

        logger.info(
            "Pool '%s': ToolManager ready (%d tools total)", pool_name, len(tm.list_tools())
        )
        return tm

    # ── Skill manager ────────────────────────────────────────────────────

    def _build_skill_manager(
        self, root_agent_name: str, project_dir: Path, pool_name: str
    ) -> Any | None:
        """Convention: skills/{pool_name}/{agent_name}/."""
        directories = [project_dir / "skills" / pool_name / root_agent_name]

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
            directories=found,
            cache=True,
            layout="directory",
            skill_filename="SKILL.md",
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

    def _fallback_context_manager(self, main_spec: AgentSpec, system_prompt: str) -> Any:
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


# ── Runtime builders (hook runner / control channel / command processor) ──


def _build_hook_runner(hooks: list[Hook[Any]]) -> HookRunner[Any]:  # type: ignore[type-arg]
    """Build a HookRunner from the provided hooks."""
    from modex_agent.hook import HookErrorPolicy, HookRunner, HookSpec

    runner = HookRunner()
    for hook in hooks:
        runner.add(HookSpec(hook=hook, on_error=HookErrorPolicy.LOG))
    return runner


def _build_control_channel(
    existing: InMemoryControlChannel | None,
) -> InMemoryControlChannel:
    """Build the control channel for control commands.

    Reuses the existing channel when already set (idempotent), otherwise
    creates a fresh :class:`InMemoryControlChannel`.
    """
    if existing is None:
        return InMemoryControlChannel()
    return existing


def _build_main_command_processor() -> SlashCommandProcessor:
    """Build the slash command processor.

    Wires the default builtin handlers.  Workspace commands (/cd,
    /exit, /pwd) are handled directly by the IM input pipeline
    (``EnvironmentControlStage``) so they are removed from the
    processor — this avoids self-blocking where the command's own
    dispatch would appear as an "active agent" in pool mode.
    """
    from modex_agent.commands.handlers import build_default_builtin_handlers
    from modex_agent.commands.processor import SlashCommandProcessor

    return SlashCommandProcessor(handlers=list(build_default_builtin_handlers()))


# ── T26: Persistence backend factory selection ──
# Each factory returns the SQLite adapter when backend == SQLITE and a
# WorkspacePersistenceManager is available, otherwise the file-based impl.


def build_inbox(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    inbox_dir: Path,
    db_path: Path,
    pool_name: str,
) -> Any:
    if (
        app_config is not None
        and persistence is not None
        and app_config.persistence.backend is PersistenceBackend.SQLITE
    ):
        from modex_agent.persistence.adapters.inbox_mq import SqliteInboxMQ

        return SqliteInboxMQ(
            db_path,
            BotRecordScope(pool=pool_name),
            connection=persistence.connection,
        )
    from modex_agent.multi_agent.inbox.server_local import LocalFileInboxMQ

    return LocalFileInboxMQ(workspace=inbox_dir)


def _is_sqlite(
    app_config: AppConfig | None,
    persistence: object | None,
) -> bool:
    """True when the SQLITE backend is selected and a manager is available."""
    return (
        app_config is not None
        and persistence is not None
        and app_config.persistence.backend is PersistenceBackend.SQLITE
    )


def build_turn_state_store(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    turns_dir: Path,
    codec_registry: RuntimeStateCodecRegistry,
) -> Any:
    if _is_sqlite(app_config, persistence):
        assert persistence is not None
        from modex_agent.persistence.adapters.turn_state_store import SqliteTurnStateStore

        return SqliteTurnStateStore(persistence.connection, codec_registry)
    from modex_agent.runtime.store import JsonFileTurnStateStore

    return JsonFileTurnStateStore(turns_dir, codec_registry)


def build_session_store(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    session_index_dir: Path,
    pool_resolver: Callable[[SessionInfo], str],
    data_dir_name: str,
) -> SessionStore:
    if _is_sqlite(app_config, persistence):
        assert persistence is not None
        from modex_agent.persistence.adapters.session_store import SqliteSessionStore

        return SqliteSessionStore(persistence.connection)
    from bot.service.session_store import WorkspacePoolSessionStore

    return WorkspacePoolSessionStore(
        base_dir=session_index_dir,
        pool_resolver=pool_resolver,
        data_dir_name=data_dir_name,
    )


def build_pool_routing_store(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    data_dir: Path,
    db_path: Path,
) -> PoolRoutingStore:
    if _is_sqlite(app_config, persistence):
        from modex_agent.persistence.adapters.pool_routing_store import SqlitePoolRoutingStore

        return SqlitePoolRoutingStore(db_path)
    from modex_agent.multi_agent.pool_router import LocalFilePoolRoutingStore

    return LocalFilePoolRoutingStore(data_dir=data_dir)


def build_external_session_map_store(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    workspace_dir: Path,
    scope: RecordScope,
) -> Any:
    if _is_sqlite(app_config, persistence):
        assert persistence is not None
        from modex_agent.persistence.adapters.external_session_map_store import (
            SqliteExternalSessionMapStore,
        )

        return SqliteExternalSessionMapStore(persistence.connection, scope)
    from modex_agent.agents.external.paths import ExternalPaths
    from modex_agent.agents.external.session_store import (
        LocalFileExternalSessionMapStore,
    )

    return LocalFileExternalSessionMapStore(ExternalPaths(workspace_dir))


def build_memory_registry(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    memory_dir: Path,
    scope: RecordScope,
) -> MemoryStoreRegistry | None:
    if _is_sqlite(app_config, persistence):
        assert persistence is not None
        from modex_agent.persistence.memory_registry import HybridMemoryStoreRegistry

        return HybridMemoryStoreRegistry(
            file_root=memory_dir,
            persistence=persistence,
            base_scope=scope,
        )
    return None


def build_workspace_registry_store(
    app_config: AppConfig | None,
    registry_persistence: RegistryPersistenceManager | None,
    home: Path,
    data_dir_name: str,
) -> ScopeRegistryStore:
    if _is_sqlite(app_config, registry_persistence):
        assert registry_persistence is not None
        return registry_persistence.store
    from modex_agent.workspace.store import GlobalWorkspaceStore

    return GlobalWorkspaceStore(home=home, data_dir_name=data_dir_name)


def build_approval_audit_store(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    scope: RecordScope,
) -> Any | None:
    if _is_sqlite(app_config, persistence):
        assert persistence is not None
        from modex_agent.persistence.adapters.approval_audit_store import (
            SqliteApprovalAuditStore,
        )

        return SqliteApprovalAuditStore(persistence.connection, scope)
    return None
