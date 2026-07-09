"""Agent builder mixin for BotService — tool registration, memory, descriptors.

All tool registration methods use Tool objects directly (code-passed).
No tool configuration is read from YAML/config dicts.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bot.plugins.integration import PluginIntegration

from modex_agent.core.skills import SkillManager
from modex_agent.core.tool_manager import Tool
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import AgentMessageBus
from modex_agent.multi_agent.comm_tracker import CommunicationTracker
from modex_agent.pipeline.adapters import OutputAdapter

if TYPE_CHECKING:
    # Annotation-only import: the registry type is referenced solely in the
    # ``_load_agent_mcp_tools`` signature (a string under ``from __future__
    # import annotations``); deferred to TYPE_CHECKING to keep the import graph
    # acyclic (the framework registry pulls in connection/injector modules).
    from modex_agent.tools.mcp.registry import McpConnectionRegistry

logger = logging.getLogger(__name__)


def resolve_system_prompt(agent_cfg: Any, project_dir: Path) -> str:
    """Resolve system prompt: agents/{name}.md if exists, else YAML value."""
    md_path = project_dir / "agents" / f"{agent_cfg.name}.md"
    if md_path.exists():
        return md_path.read_text(encoding="utf-8")
    return getattr(agent_cfg, "system_prompt", "")


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
    from modex_agent.ioc.configs.app import _resolve_env_in
    from modex_agent.tools.mcp import MCPClientManager

    from bot.config.mcp_registry import resolve_agent_mcp_servers

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

    communication_tracker: CommunicationTracker | None
    plugin_integration: PluginIntegration | None

    # Subagent caches
    _subagent_skill_managers: dict[str, SkillManager]
    _subagent_memory_systems: dict[str, Any]
    _additional_subagent_memory_systems: dict[str, Any]

    # ── Properties provided by BotService ──

    @property
    def _project_dir(self) -> Path:
        """Project root directory. Implemented by BotService."""
        raise NotImplementedError
