"""Default tool factories for preset tools and capability-backed extras.

The stateless standard tools (read/write/edit/ls/glob/grep/web_*,
aci_edit, ast_grep_*) use ``PrototypeFactory`` — a fresh instance per
assembly, so no agent ever shares a mutable ``Tool`` instance (a shared
instance would leak ``register(tool, config)`` mutations and future
per-tool config across agents/pools/workspaces). Todo, experience, and
bash tools use runtime factories because their construction depends on
pool-scoped runtime objects (the capability supplies' TodoStore /
experience dir + meta store, terminal manager). The capability-backed
names (``aci_edit``, ``ast_grep_*``, ``todo_*``, ``experience``) are
registered under their own names for their capability packages to
contribute into rosters. The ACI edit upgrade is registered under
``aci_edit`` so the ``aci`` capability package can contribute it into
rosters with the ``edit ← aci_edit`` O3 replacement (``edit`` stays the
plain EditFileTool for agents without the capability); the ast_grep
search/replace pair is registered under its own names for the
``ast_grep`` capability package (tools-only contribution, no
replacement).

Communication tools (task/send_to_peer/send_to_agent) live in
:mod:`modex_agent.plugins.defaults.communication` as TOOL-slot factories
resolved when a compiled scope spec carries the derived entries (SPEC
§5.2, ticket 07). The legacy roster road — including its conditional
registration — is deleted; the derived entries are the only road.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from pydantic import BaseModel

from modex_agent.core.tool_manager import Tool
from modex_agent.plugins.abc import ComponentFactory, PrototypeFactory
from modex_agent.plugins.assembly.context import PoolContext, PoolRuntimeDeps
from modex_agent.plugins.defaults.capabilities.todo import require_todo_supply
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.sandbox.shell_plan import (
    ShellAssemblyDeps,
    build_bash_tool,
    resolved_binding,
)
from modex_agent.tools.presets import (
    make_aci_edit_tool,
    make_ast_grep_replace_tool,
    make_ast_grep_search_tool,
)
from modex_agent.tools.standard import (
    EditFileTool,
    GlobTool,
    ListDirTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from modex_agent.tools.standard.todo_tool import TodoReadTool, TodoWriteTool
from modex_agent.tools.terminal import (
    ProcessRegistry,
    ProcessTool,
    TerminalTool,
)
from modex_agent.tools.terminal.managers import TerminalManagerBase
from modex_agent.tools.terminal.persistent_bash import (
    persistent_bash_supported,
)
from modex_agent.tools.web import WebReaderTool, WebSearchTool

__all__ = ["ToolConfig", "register_default_tools"]

logger = logging.getLogger(__name__)


class ToolConfig(BaseModel):
    """Empty config for default tool factories."""

    model_config = {"frozen": True, "extra": "forbid"}


_STANDARD_TOOL_BUILDERS: dict[str, Callable[[], Tool]] = {
    "read": ReadFileTool,
    "write": WriteFileTool,
    "edit": EditFileTool,
    "ls": ListDirTool,
    "glob": GlobTool,
    "grep": SearchFilesTool,
    "web_search": WebSearchTool,
    "web_reader": WebReaderTool,
    "aci_edit": make_aci_edit_tool,
    "ast_grep_search": make_ast_grep_search_tool,
    "ast_grep_replace": make_ast_grep_replace_tool,
}
"""Registry name → zero-arg builder for the stateless standard tools.

The set is the union of every ``ToolPreset`` expansion plus the
capability-backed upgrades (``aci_edit`` / ``ast_grep_*``); ``bash``
is absent — its roster name resolves through the terminal-aware
``BashToolFactory`` below."""


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


async def _shell_assembly_deps(
    pool_runtime: PoolRuntimeDeps | None,
    pty_supported: bool,
    terminal_pair: tuple[TerminalManagerBase, ProcessRegistry] | None,
) -> ShellAssemblyDeps:
    """Group the pool's bash-slot assembly inputs into the typed carrier.

    ``resolved_binding`` reads the chain's shared execution owner, so a
    confirmed startup fallback is visible to both tools and telemetry.
    """
    binding = None
    if pool_runtime is not None:
        binding = await resolved_binding(pool_runtime.interceptor_chain)
    return ShellAssemblyDeps(
        binding=binding,
        terminal_pair=terminal_pair,
        persistent_bash=(pool_runtime.persistent_bash if pool_runtime is not None else None),
        root_provider=(pool_runtime.root_provider if pool_runtime is not None else None),
        pty_supported=pty_supported,
    )


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
    """The roster ``bash`` slot, delegated to the shell execution plan.

    Declares ``PoolContext`` — the terminal manager and its pool-unique
    process registry are pool-layer data.

    The construction decision lives in
    :func:`modex_agent.sandbox.shell_plan.build_bash_tool` (the single
    factory): a FULL sandbox substrate never reuses the host terminal
    trio or the strategy-supplied host persistent shell — the tool runs
    through the resolved sandbox argv or assembly fails. HOST / no-guard
    keeps the historical host behavior: prefer a terminal pair; without
    one, use a stateless subprocess on hosts without POSIX PTY support,
    otherwise reuse or create a persistent shell. Only a persistent shell
    carries cwd/env across calls and receives a matching ``bash_input``
    companion through native assembly.
    """

    config_model = ToolConfig

    async def create(self, config: BaseModel, ctx: PoolContext) -> Tool:
        del config
        terminal_pair = _pool_terminal_pair(ctx.pool_runtime)
        deps = await _shell_assembly_deps(
            ctx.pool_runtime, persistent_bash_supported(), terminal_pair
        )
        return build_bash_tool(deps)


class ProcessToolFactory(ComponentFactory):
    """Pool-runtime ``process`` tool — explicit roster opt-in only.

    Declares ``PoolContext`` (terminal manager + process registry).

    NOT part of preset auto-expansion: resolves only when a roster lists
    ``process`` in ``tools``. Terminal unavailable → ValueError (the
    roster explicitly asked for the tool; silent degradation would hide
    the mistake).

    This trio companion stays bound to the host terminal manager even
    under a FULL sandbox substrate — its ``bash``-slot sibling is the
    sandboxed shell (see ``BashToolFactory``); this tool answers the
    host trio's own tabs and never represents the sandboxed bash
    session.
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
    Like ``process``, stays host-bound under a FULL sandbox substrate —
    its tabs are the host trio's, never the sandboxed bash session.
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


def register_default_tools(ctx: PluginRegistrationContext) -> None:
    """Register the stateless standard tools plus the runtime and
    capability-backed tool factories."""
    # Stateless standard tools: prototype semantics — one fresh instance
    # per assembly. A preset-union-derived singleton here previously
    # shared one mutable Tool object across every agent/pool/workspace.
    for name, builder in _STANDARD_TOOL_BUILDERS.items():
        ctx.register_tool(name, PrototypeFactory(builder, config_model=ToolConfig))

    # Bash: a runtime factory (terminal-manager aware), registered under the
    # name preset expansion emits (scope.derivation._expand_preset_tool_names).
    ctx.register_tool("bash", BashToolFactory())

    # Terminal trio companions of bash: explicit roster opt-in only
    # (NOT preset-expanded — presets name "bash" alone; a roster lists
    # "process"/"terminal" in tools: [...] to opt in, e.g. subagents that
    # need interactive input). Both share the pool-unique ProcessRegistry.
    ctx.register_tool("process", ProcessToolFactory())
    ctx.register_tool("terminal", TerminalToolFactory())

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

    # Experience tool: the ``experience`` capability package registers
    # this name itself (its single registration entry also covers the
    # capability + review hook) — nothing experience-owned here.
