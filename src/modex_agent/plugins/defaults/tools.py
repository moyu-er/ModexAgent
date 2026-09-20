"""Default tool factories for preset tools and capability-backed extras.

The stateless standard tools (read/write/edit/ls/glob/grep/web_*,
aci_edit, ast_grep_*) use ``PrototypeFactory`` — a fresh instance per
assembly, so no agent ever shares a mutable ``Tool`` instance (a shared
instance would leak ``register(tool, config)`` mutations and future
per-tool config across agents/pools/workspaces). Todo, experience, and
shell tools use runtime factories because their construction depends on
capability-owned runtime wiring. The capability-backed
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

from collections.abc import Callable

from pydantic import BaseModel

from modex_agent.core.tool_manager import Tool
from modex_agent.plugins.abc import ComponentFactory, PrototypeFactory
from modex_agent.plugins.assembly.context import PoolContext
from modex_agent.plugins.defaults.capabilities.shell.factory import (
    ShellToolGroupFactory,
)
from modex_agent.plugins.defaults.capabilities.todo import require_todo_supply
from modex_agent.plugins.loader import PluginRegistrationContext
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
from modex_agent.tools.web import WebReaderTool, WebSearchTool

__all__ = ["ToolConfig", "register_default_tools"]


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
is absent — its roster name resolves through the capability-owned
``ShellToolGroupFactory`` below."""


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


def register_default_tools(ctx: PluginRegistrationContext) -> None:
    """Register the stateless standard tools plus the runtime and
    capability-backed tool factories."""
    # Stateless standard tools: prototype semantics — one fresh instance
    # per assembly. A preset-union-derived singleton here previously
    # shared one mutable Tool object across every agent/pool/workspace.
    for name, builder in _STANDARD_TOOL_BUILDERS.items():
        ctx.register_tool(name, PrototypeFactory(builder, config_model=ToolConfig))

    # One anchor factory returns the capability-selected atomic shell group.
    # Companions are members of that group, never independent registrations.
    ctx.register_tool("bash", ShellToolGroupFactory())

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
