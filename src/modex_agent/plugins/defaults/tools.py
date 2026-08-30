"""Default tool factories for preset tools and capability-backed extras.

Stateless preset and ast_grep tools use ``SimpleFactory``. Todo,
experience, and bash tools use runtime factories because their
construction depends on pool-scoped runtime objects (the capability
supplies' TodoStore / experience dir + meta store, terminal manager). Preset names project from
``ToolPreset``; the capability-backed names (``aci_edit``,
``ast_grep_*``, ``todo_*``, ``experience``) are registered under their
own names for their capability packages to contribute into rosters.
The ACI edit upgrade is registered under ``aci_edit`` so the ``aci``
capability package can contribute it into rosters with the
``edit ← aci_edit`` O3 replacement (``edit`` stays the plain EditFileTool
for agents without the capability); the ast_grep search/replace pair is
registered under its own names for the ``ast_grep`` capability package
(tools-only contribution, no replacement).

Communication tools (task/send_to_peer/send_to_agent) live in
:mod:`modex_agent.plugins.defaults.communication` as TOOL-slot factories
resolved when a compiled scope spec carries the derived entries (SPEC
§5.2, ticket 07). The legacy roster road — including its conditional
registration — is deleted; the derived entries are the only road.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict

from modex_agent.core.tool_manager import Tool
from modex_agent.memory.tools.experience import ExperienceTool
from modex_agent.plugins.abc import ComponentFactory, SimpleFactory
from modex_agent.plugins.assembly.context import PoolContext, PoolRuntimeDeps
from modex_agent.plugins.defaults.capabilities.todo import require_todo_supply
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.tools.presets import (
    ToolPreset,
    get_preset_tools,
    make_aci_edit_tool,
    make_ast_grep_tools,
)
from modex_agent.tools.standard.todo_tool import TodoReadTool, TodoWriteTool
from modex_agent.tools.terminal import (
    CommandTool,
    ProcessRegistry,
    ProcessTool,
    TerminalTool,
)
from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.managers import TerminalManagerBase
from modex_agent.tools.terminal.persistent_bash import (
    PersistentBashTool,
    persistent_bash_supported,
)

__all__ = ["ToolConfig", "register_default_tools"]

logger = logging.getLogger(__name__)


class ToolConfig(BaseModel):
    """Empty config for default tool factories."""

    model_config = {"frozen": True, "extra": "forbid"}


def _pool_terminal_pair(
    pool_runtime: PoolRuntimeDeps | None,
) -> tuple[TerminalManagerBase, ProcessRegistry] | None:
    """Return ``(terminal_manager, process_registry)`` for terminal pools.

    ``None`` means the pool has no terminal manager (bash degrades to the
    persistent shell; process/terminal factories raise). A manager without
    its registry is a broken PoolAssembleStage invariant — fail fast
    instead of silently forking the process bookkeeping.
    """
    if pool_runtime is None or pool_runtime.terminal_manager is None:
        return None
    if pool_runtime.process_registry is None:
        raise ValueError(
            "pool_runtime.process_registry must be set whenever "
            "terminal_manager is (PoolAssembleStage enforces this invariant)"
        )
    return pool_runtime.terminal_manager, pool_runtime.process_registry


def _fallback_persistent_bash(pool_runtime: PoolRuntimeDeps | None) -> Tool:
    """No-terminal-manager bash fallback: the pool's persistent shell.

    ``pool_runtime.persistent_bash`` (a strategy-supplied pool shell) is
    returned when present; otherwise a fresh lazy shell rooted at the
    pool's workspace when known. Hosts without a POSIX pty (Windows) get
    the stateless :class:`SubprocessTool` instead — the persistent shell
    cannot spawn there.
    """
    if not persistent_bash_supported():
        logger.warning(
            "bash fallback: POSIX pty unavailable on this host; "
            "bash falls back to SubprocessTool (stateless)"
        )
        from modex_agent.tools.terminal.subprocess_tool import (
            SubprocessTool,
            create_subprocess_executor,
        )

        return SubprocessTool(executor=create_subprocess_executor(), timeout=300)
    if pool_runtime is not None and pool_runtime.persistent_bash is not None:
        return pool_runtime.persistent_bash
    root_provider = pool_runtime.root_provider if pool_runtime is not None else None
    initial_cwd = str(root_provider.current()) if root_provider is not None else None
    # max_output_chars=None: truncation is interceptor-owned — every
    # assembly road wires ToolResultLimitInterceptor (50K), and a self-
    # clipping shell would truncate BEFORE the interceptor sees the full
    # output (the 16K class default is for direct constructors only).
    return PersistentBashTool(initial_cwd=initial_cwd, max_output_chars=None)


class TodoToolFactory(ComponentFactory):
    """Todo tools from the pool layer (SPEC §3.3 example factory).

    Declares ``PoolContext`` — the narrowest layer holding the todo
    supply; workspace-layer fields are a type error for this factory.
    The store comes from the pool's ``capability_supply['todo']``
    (:class:`~modex_agent.plugins.defaults.capabilities.todo.TodoSupply`,
    built by ``TodoCapability.supply`` iff the capability is effective in
    the pool) — missing/wrong-typed supply raises loudly.
    """

    config_model = ToolConfig

    def __init__(self, tool_type: type[TodoWriteTool] | type[TodoReadTool]) -> None:
        self._tool_type = tool_type

    async def create(self, config: BaseModel, ctx: PoolContext) -> Tool:
        del config
        supply = require_todo_supply(ctx.pool_runtime)
        return self._tool_type(supply.store)


class BashToolFactory(ComponentFactory):
    """Terminal-manager-aware bash tool (presets.py bash gating).

    Declares ``PoolContext`` — the terminal manager and its pool-unique
    process registry are pool-layer data.

    Produces ``CommandTool`` bound to the POOL-UNIQUE ``ProcessRegistry``
    (``pool_runtime.process_registry``) when the pool runtime supplies a
    terminal manager — sharing the pool registry with the process/terminal
    tools is what keeps interactive writes resolvable (a private registry
    here previously forked the process bookkeeping: bash registered
    commands in one registry, ``process write`` consulted another).
    Without a terminal manager the bash slot is the pool's persistent
    shell (:class:`PersistentBashTool`) — stateful cwd/env across calls,
    with the strategy-registered ``bash_input`` companion answering
    stdin-waiting commands.
    """

    config_model = ToolConfig

    async def create(self, config: BaseModel, ctx: PoolContext) -> Tool:
        del config
        pair = _pool_terminal_pair(ctx.pool_runtime)
        if pair is None:
            return _fallback_persistent_bash(ctx.pool_runtime)
        terminal_manager, process_registry = pair
        return CommandTool(
            manager=terminal_manager,
            registry=process_registry,
            config=TerminalRuntimeConfig(),
        )


class ProcessToolFactory(ComponentFactory):
    """Pool-runtime ``process`` tool — explicit roster opt-in only.

    Declares ``PoolContext`` (terminal manager + process registry).

    NOT part of preset auto-expansion: resolves only when a roster lists
    ``process`` in ``tools``. Terminal unavailable → ValueError (the
    roster explicitly asked for the tool; silent degradation would hide
    the mistake).
    """

    config_model = ToolConfig

    async def create(self, config: BaseModel, ctx: PoolContext) -> Tool:
        del config
        pair = _pool_terminal_pair(ctx.pool_runtime)
        if pair is None:
            raise ValueError(
                "pool_runtime.terminal_manager is required; enable terminal "
                "support (use_terminal) for this pool to use the process tool"
            )
        terminal_manager, process_registry = pair
        return ProcessTool(registry=process_registry, manager=terminal_manager)


class TerminalToolFactory(ComponentFactory):
    """Pool-runtime ``terminal`` tool — explicit roster opt-in only.

    Declares ``PoolContext`` (terminal manager + process registry).

    Same availability contract as :class:`ProcessToolFactory`; injects the
    pool registry so ``terminal list``/``current`` report running commands.
    """

    config_model = ToolConfig

    async def create(self, config: BaseModel, ctx: PoolContext) -> Tool:
        del config
        pair = _pool_terminal_pair(ctx.pool_runtime)
        if pair is None:
            raise ValueError(
                "pool_runtime.terminal_manager is required; enable terminal "
                "support (use_terminal) for this pool to use the terminal tool"
            )
        terminal_manager, process_registry = pair
        return TerminalTool(terminal_manager, registry=process_registry)


class ExperienceToolConfig(BaseModel):
    """Config for :class:`ExperienceToolFactory` — no settings.

    The experience directory is a pool-layer resource extracted from
    ``ctx.pool_runtime`` at ``create()`` time, not carried by config.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class ExperienceToolFactory(ComponentFactory):
    """Experience tool from the pool layer (moved from the bot plugin).

    Declares ``PoolContext`` — the experience directory and its metadata
    store are the pool's ``experience`` capability supply
    (:meth:`ExperienceCapability.supply` builds them iff the capability
    is effective in the pool). Missing supply fails loudly — a
    roster-referenced component is never silently skipped (the bare
    ``tools: [+experience]`` degraded mode hits this raise: the tool name
    alone never built a supply).
    """

    config_model = ExperienceToolConfig

    async def create(self, config: BaseModel, ctx: PoolContext) -> Tool:
        from modex_agent.plugins.defaults.capabilities.experience import (
            require_experience_supply,
        )

        del config
        supply = require_experience_supply(ctx.pool_runtime)
        return ExperienceTool(supply.experience_dir, supply.meta_store)


def register_default_tools(ctx: PluginRegistrationContext) -> None:
    """Register the dynamically projected preset tool union plus the
    runtime and capability-backed tool factories."""
    # Presets iterate FIRST so a later registration colliding with a
    # preset name cannot shadow the preset implementation ("edit" stays
    # the plain EditFileTool; the ACI upgrade is registered under its own
    # name).
    seen: dict[str, Tool] = {}
    for preset in ToolPreset:
        for tool in get_preset_tools(preset):
            seen.setdefault(tool.name, tool)
    for name, tool in seen.items():
        ctx.register_tool(name, SimpleFactory(instance=tool, config_model=ToolConfig))

    # Bash: a runtime factory (terminal-manager aware), registered under the
    # name preset expansion emits (scope.derivation._expand_preset_tool_names).
    ctx.register_tool("bash", BashToolFactory())

    # Terminal trio companions of bash: explicit roster opt-in only
    # (NOT preset-expanded — presets name "bash" alone; a roster lists
    # "process"/"terminal" in tools: [...] to opt in, e.g. subagents that
    # need interactive input). Both share the pool-unique ProcessRegistry.
    ctx.register_tool("process", ProcessToolFactory())
    ctx.register_tool("terminal", TerminalToolFactory())

    # ACI edit upgrade: distinct registry name so the ``aci`` capability
    # (plugins/defaults/capabilities/aci.py) can contribute it into rosters
    # with the ``edit ← aci_edit`` O3 replacement; the tool's own
    # LLM-facing name stays "edit".
    ctx.register_tool(
        "aci_edit", SimpleFactory(instance=make_aci_edit_tool(), config_model=ToolConfig)
    )

    # ast_grep pair: the ``ast_grep`` capability
    # (plugins/defaults/capabilities/ast_grep.py) contributes these registry
    # names into rosters; the tools resolve through the regular TOOL slot.
    for tool in make_ast_grep_tools():
        ctx.register_tool(tool.name, SimpleFactory(instance=tool, config_model=ToolConfig))

    # Todo pair: the ``todo`` capability
    # (plugins/defaults/capabilities/todo.py) contributes these registry
    # names into rosters; the tools resolve through the regular TOOL slot
    # against the pool's todo supply. Registered by NAME (an explicit
    # name→factory map) so a reorder can never swap read/write.
    todo_factories: dict[str, TodoToolFactory] = {
        "todo_write": TodoToolFactory(TodoWriteTool),
        "todo_read": TodoToolFactory(TodoReadTool),
    }
    for name, factory in todo_factories.items():
        ctx.register_tool(name, factory)

    # Experience tool: the ``experience`` capability
    # (plugins/defaults/capabilities/experience.py) contributes this
    # registry name into rosters; the pool-data-fed factory builds the
    # tool at assembly time.
    ctx.register_tool("experience", ExperienceToolFactory())
