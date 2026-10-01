"""Agent build helpers for the bot service layer.

The persistence backend factories, the pool-assembly helpers
(``_PoolAssemblyMixin``), and ``resolve_declared_root_prompt`` were
promoted into the framework (``modex_agent.plugins.assembly.backend_factory``
/ ``pool_factory``, W4a). What remains here: the ``AgentBuilderMixin`` for
BotService, the runtime builders (hook runner / control channel / command
processor), and the bot's long-term-memory default templates.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.adapters.output import OutputAdapter
from modex_agent.app.config import AppConfig
from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.core.tool_manager import (
    Tool,
)
from modex_agent.hook.abc import Hook
from modex_agent.memory.config import MemoryConfig
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.scope import MemoryContext
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import AgentMessageBus

if TYPE_CHECKING:
    from modex_agent.commands.processor import SlashCommandProcessor
    from modex_agent.hook.runner import HookRunner

logger = logging.getLogger(__name__)


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
    _subagent_memory_systems: dict[str, Any]
    _additional_subagent_memory_systems: dict[str, Any]

    _transcript_store: Any | None = None

    # ── Properties provided by BotService ──

    @property
    def _project_dir(self) -> Path:
        """Project root directory. Implemented by BotService."""
        raise NotImplementedError


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


# ── Long-term memory defaults (moved from pool/pool_construction.py, W4a) ──


async def ensure_long_term_defaults(
    project_dir: Path,
    memory_cfg: MemoryConfig | None,
    memory_system: DefaultMemorySystem,
) -> None:
    """Initialize default long-term memory files if core memory is enabled.

    Supports both old ``long_term`` config (deprecated) and new ``core``
    config. Template paths in config are relative to the project directory.
    Resolves them to absolute paths before calling ``ensure_defaults`` so
    the core memory layer finds templates regardless of CWD (critical after
    ``/cd`` switches the conversation to a different workspace).

    Bot-owned: the default soul/user/memory templates are business content;
    the framework pool factory does not read them.
    """
    if memory_cfg is None:
        return

    core_enabled = False
    if memory_cfg.long_term is not None and memory_cfg.long_term.enabled:
        core_enabled = True
    if memory_cfg.core is not None and memory_cfg.core.enabled:
        core_enabled = True
    if not core_enabled:
        return

    lt_mgr = memory_system.core_memory_manager
    if lt_mgr is None:
        return

    raw_template_dir: str | None = None
    if memory_cfg.core is not None:
        raw_template_dir = memory_cfg.core.default_templates_dir
    if not raw_template_dir and memory_cfg.long_term is not None:
        raw_template_dir = memory_cfg.long_term.default_templates_dir
    if raw_template_dir:
        abs_template_dir = str((project_dir / raw_template_dir).resolve())
        lt_mgr._config = lt_mgr._config.model_copy(
            update={"default_templates_dir": abs_template_dir}
        )

    defaults: dict[str, str] = {
        "soul": (
            "## Communication style\n"
            "- Reply in Chinese, natural and concise\n"
            "- Give the direct answer first, then add explanation\n"
            "- State uncertain things honestly; never fabricate\n"
        ),
        "user": (
            "## User profile\n- First use; no specific preference records yet\n- User habits and preferences accumulate over later conversations\n"
        ),
        "memory": ("## Relevant knowledge\n- No specific domain-knowledge records yet\n- Automatically organized and updated over long-term conversations\n"),
    }

    ctx = MemoryContext(session_id="default", user_id="default")
    await lt_mgr.ensure_defaults(ctx, defaults)
    logger.info("Long-term memory defaults ensured")
