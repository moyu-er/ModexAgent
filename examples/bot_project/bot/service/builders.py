"""Agent builder mixin for BotService — tool registration, memory, descriptors.

All tool registration methods use Tool objects directly (code-passed).
No tool configuration is read from YAML/config dicts.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bot.plugins.integration import PluginIntegration
from modex_agent.core.scope import RecordScope
from modex_agent.core.session_store import SessionStore
from modex_agent.core.skills import SkillManager
from modex_agent.core.tool_manager import Tool
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import AgentMessageBus
from modex_agent.multi_agent.pool_router import PoolRoutingStore
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.pipeline.adapters import OutputAdapter
from modex_agent.workspace.registry import WorkspaceRegistryStore

if TYPE_CHECKING:
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.memory.registry import MemoryStoreRegistry
    from modex_agent.persistence.managers import (
        RegistryPersistenceManager,
        WorkspacePersistenceManager,
    )
    from modex_agent.runtime.codec import RuntimeStateCodecRegistry
    from modex_agent.tools.mcp.registry import McpConnectionRegistry

logger = logging.getLogger(__name__)


def resolve_system_prompt(agent_name: str, project_dir: Path) -> str:
    """Resolve system prompt: agents/{name}.md if exists, else empty string."""
    md_path = project_dir / "agents" / f"{agent_name}.md"
    if md_path.exists():
        return md_path.read_text(encoding="utf-8")
    return ""


# ── Standard tool builders (code objects, no config) ──


def _make_file_tools() -> list[Tool]:
    from modex_agent.tools.standard import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool

    return [ReadFileTool(), WriteFileTool(), EditFileTool(), ListDirTool()]


def _make_shell_tool(
    terminal_manager: Any | None = None,
    timeout: int = 300,
) -> Tool:
    from modex_agent.tools.terminal import SubprocessExecutor, SubprocessTool

    return SubprocessTool(executor=SubprocessExecutor(), timeout=timeout)


def _make_search_tools() -> list[Tool]:
    from modex_agent.tools.standard import FindFilesTool, SearchFilesTool

    return [SearchFilesTool(), FindFilesTool()]


# ── MCP tool helpers ──


async def _load_agent_mcp_tools(
    agent_name: str,
    selection: list[str],
    project_dir: Path,
    *,
    mcp_registry: McpConnectionRegistry | None = None,
) -> tuple[list[Tool], Any | None]:
    """Load MCP tools for an agent from its registry selection.

    Resolves ``selection`` (server names) against ``config/mcp/registry.json``
    via :mod:`bot.config.mcp_registry`, then connects and adapts the tools.

    When ``mcp_registry`` is given (ADR-0017 Task 5a, main-agent path), the
    selection is obtained from the shared :class:`McpConnectionRegistry` as a
    :class:`SharedMcpBackend` facade — no private ``MCPClientManager`` is built,
    and no ``registry.json`` is read here (the registry already holds the full
    server map). When ``mcp_registry`` is ``None``, today's per-pool path runs
    byte-for-byte (resolve → ``MCPClientManager`` → ``initialize``).

    Returns ``(tools, mcp_manager)`` — the manager must be kept alive for
    connection lifecycle. Both ``MCPClientManager`` and ``SharedMcpBackend``
    are ``McpBackend`` and expose ``release()``, so teardown is uniform.
    """
    from modex_agent.tools.mcp_adapter import acquire_mcp_tools

    if not selection:
        return [], None

    # ── Shared-registry branch (ADR-0017): acquire a facade, no per-pool connect ──
    if mcp_registry is not None:
        try:
            backend = await mcp_registry.acquire(selection)
        except Exception as exc:  # noqa: BLE001 - fail-soft: MCP must never break the pool
            logger.warning(
                "Agent %s: shared MCP acquire failed: %s", agent_name, exc
            )
            return [], None

        # Diagnostic: reveal which shared-registry servers were READY at this
        # acquisition. Empty here ⇒ empty tools below. MCP availability is the
        # READY-snapshot at materialization time, so this line is the key signal
        # for "MCP missing after workspace switch" (home vs non-home differ only
        # in WHEN they acquire — a server that dropped in between is absent).
        logger.info(
            "Agent %s: shared MCP acquire connected_servers=%s (selection=%s)",
            agent_name, backend.connected_servers, selection,
        )

        tools = await acquire_mcp_tools(backend, tool_timeout=60)
        logger.info(
            "Agent %s: %d MCP tools loaded from selection %s",
            agent_name, len(tools), selection,
        )
        return tools, backend

    # ── Legacy per-pool branch (flag-off / non-registry world): byte-for-byte ──
    from bot.config.mcp_registry import resolve_agent_mcp_servers
    from modex_agent.ioc.configs.app import _resolve_env_in
    from modex_agent.tools.mcp import MCPClientManager

    registry_path = project_dir / "config" / "mcp" / "registry.json"
    try:
        servers = resolve_agent_mcp_servers(selection, registry_path)
    except Exception as e:
        logger.warning("Agent %s: MCP selection %s resolve failed: %s", agent_name, selection, e)
        return [], None

    if not servers:
        return [], None

    try:
        servers = _resolve_env_in(servers)
        manager = MCPClientManager(config=servers)
        await manager.initialize()

        if not manager.connected_servers:
            logger.warning("Agent %s: MCP config loaded but no servers connected", agent_name)
            return [], manager

        tools = await acquire_mcp_tools(manager, tool_timeout=60)
        logger.info(
            "Agent %s: %d MCP tools loaded from selection %s",
            agent_name, len(tools), selection,
        )
        return tools, manager

    except Exception as e:
        logger.warning("Failed to load MCP tools for agent %s: %s", agent_name, e)
        return [], None


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

    plugin_integration: PluginIntegration | None

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
            RecordScope(pool=pool_name),
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

        return SqliteSessionStore(persistence.connection, pool_resolver=pool_resolver)
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
    from modex_agent.agents.external_coding.paths import ExternalPaths
    from modex_agent.agents.external_coding.session_store import (
        LocalFileExternalSessionMapStore,
    )

    return LocalFileExternalSessionMapStore(ExternalPaths(workspace_dir))


def build_todo_store(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    todo_dir: Path,
    scope: RecordScope,
) -> Any:
    if _is_sqlite(app_config, persistence):
        assert persistence is not None
        from modex_agent.persistence.adapters.todo_store import SqliteTodoStore

        return SqliteTodoStore(persistence.connection, scope)
    from modex_agent.runtime.store import JsonFileTodoStore

    return JsonFileTodoStore(todo_dir)


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
) -> WorkspaceRegistryStore:
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
