"""Workspace-scoped tool wrappers.

The default standard tools (``read``/``write``/``edit``/``ls``/``glob``/
``search``/``bash``) resolve relative paths — including ``.`` — against the
**process CWD** (``os.getcwd()``). They are intentionally path-agnostic at
the framework level (see spec §5 "路径不可感知原则"): they must *consume* a
base point, never *derive* it from ``os.getcwd()``.

This module adds that base point as a layer **on top of** the default tools,
without modifying their implementations. A dynamic ``WorkspaceRootProvider``
is read at every ``execute()`` call, so a workspace switch takes effect for
all wrapped tools immediately — no per-switch wiring.

The provider is the single extension point toward the future
``WorkspaceManager`` (spec §11): today it returns
``WorkspaceContext.current``; tomorrow it returns ``manager.active.target``.
The swap is one implementation; callers and wrappers stay unchanged.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.core.tool_manager import Tool

if TYPE_CHECKING:
    from modex_agent.core.capabilities import ModelCapabilities


class WorkspaceRootProvider(ABC):
    """Provides the active workspace working directory (the ``target`` the
    user ``/cd``'d into — the agent's working dir, NOT the ``.modex`` data
    root).

    Implementations must be cheap and read live state, since it is called
    on every tool execution.
    """

    @abstractmethod
    def current(self) -> Path:
        """Return the absolute path of the active workspace working dir."""
        ...


def _is_absolute_or_home(path: str) -> bool:
    """True if ``path`` is absolute or a ``~`` expansion that the inner tool
    should resolve on its own (we must not prefix these with the base)."""
    return path.startswith("~") or Path(path).is_absolute()


class WorkspaceScopedTool(Tool):
    """Base wrapper: delegates the full ``Tool`` surface to ``inner`` and
    only overrides ``execute`` to rewrite path-like arguments against the
    active workspace root.

    Subclasses implement ``_scoped_args`` to rewrite the specific argument
    name(s) their inner tool uses.
    """

    def __init__(self, inner: Tool, root_provider: WorkspaceRootProvider) -> None:
        # Do NOT call Tool.__init__ with name/description/parameters — those
        # are delegated to ``inner`` via the properties below. We still need
        # a ``config`` attribute; reuse the inner's so timeouts etc. carry
        # over to the ToolManager.
        self._inner = inner
        self._root_provider = root_provider
        self.config = inner.config

    # -- delegated Tool surface -------------------------------------------

    @property
    def inner(self) -> Tool:
        return self._inner

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def description(self) -> str:
        return self._inner.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._inner.parameters

    def get_schema(self) -> dict[str, Any]:
        return self._inner.get_schema()

    def get_dynamic_schema(self) -> dict[str, Any]:
        return self._inner.get_dynamic_schema()

    def get_dynamic_schema_for(
        self, caps: ModelCapabilities | None = None
    ) -> dict[str, Any]:
        """Delegate to the inner tool's caps-aware override.

        Without this, a workspace-wrapped ``ReadFileTool`` would hit the
        default ``get_dynamic_schema_for`` (→ ``get_dynamic_schema`` → static
        schema), bypassing the caps-aware description the inner tool provides.
        """
        return self._inner.get_dynamic_schema_for(caps)

    # -- the only behaviour we change ------------------------------------

    def _scoped_args(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Rewrite path-like arguments against the workspace root.

        Subclasses override to name the argument(s) to rewrite.
        """
        raise NotImplementedError

    async def execute(self, **kwargs: Any) -> Any:
        return await self._inner.execute(**self._scoped_args(dict(kwargs)))


class WorkspaceScopedFileTool(WorkspaceScopedTool):
    """Wraps file/search tools (``read``/``write``/``edit``/``ls``/``glob``/
    ``grep``) whose path argument is ``path``.

    Rewrite rule for ``path``:
      - missing / ``None`` / empty / ``"."`` → the workspace root
      - relative (not absolute, not ``~``) → ``<root>/<path>``
      - absolute or ``~`` → untouched (inner tool resolves it)
    """

    _PATH_ARG = "path"

    def _scoped_args(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._PATH_ARG not in arguments:
            arguments[self._PATH_ARG] = str(self._root_provider.current())
            return arguments
        raw = arguments[self._PATH_ARG]
        if raw is None:
            arguments[self._PATH_ARG] = str(self._root_provider.current())
            return arguments
        raw_str = str(raw).strip()
        if raw_str == "" or raw_str == ".":
            arguments[self._PATH_ARG] = str(self._root_provider.current())
            return arguments
        if _is_absolute_or_home(raw_str):
            return arguments
        arguments[self._PATH_ARG] = str(self._root_provider.current() / raw_str)
        return arguments


class WorkspaceScopedShellTool(WorkspaceScopedTool):
    """Wraps ``SubprocessTool`` (``bash``), whose cwd argument is
    ``working_dir``.

    Rewrite rule: if ``working_dir`` is missing/``None``, default it to the
    workspace root. Explicit values (absolute or relative) are left for the
    inner tool to resolve — matching its existing ``working_dir or
    os.getcwd()`` contract, but with the workspace root as the default.
    """

    _CWD_ARG = "working_dir"

    def _scoped_args(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments.get(self._CWD_ARG) is None:
            arguments[self._CWD_ARG] = str(self._root_provider.current())
        return arguments


# Names whose ``path`` argument should be scoped as a file/search root.
# (``SearchFilesTool`` exposes itself as ``grep`` and ``GlobTool`` as
# ``glob`` — both take a ``path`` that defaults to ``.``.)
_FILE_TOOL_NAMES = frozenset({"read", "write", "edit", "ls", "glob", "grep"})


def _declares_working_dir(tool: Tool) -> bool:
    """True if the tool declares a ``working_dir`` parameter in its schema.

    Both ``SubprocessTool`` and ``CommandTool`` are named ``bash``, but only
    ``SubprocessTool`` takes ``working_dir`` — ``CommandTool`` runs inside a
    persistent terminal session whose cwd is bound at the manager level, not
    via a tool argument. Routing on the declared schema (not on the name)
    avoids wrapping ``CommandTool`` here.
    """
    props = tool.parameters.get("properties", {})
    return "working_dir" in props


def wrap_standard_tools(
    tools: list[Tool], root_provider: WorkspaceRootProvider
) -> list[Tool]:
    """Wrap each standard tool that resolves paths against the process CWD.

    Routing:
      - file/search tools (``read``/``write``/``edit``/``ls``/``glob``/
        ``grep``) → ``WorkspaceScopedFileTool`` (rewrites ``path``);
      - any tool declaring a ``working_dir`` argument (``SubprocessTool``)
        → ``WorkspaceScopedShellTool``;
      - everything else (``CommandTool``/``ProcessTool``/``TerminalTool``/
        MCP tools) is returned unchanged — their cwd is managed elsewhere.

    Wrapping is idempotent: an already-scoped tool is not re-wrapped.
    """
    scoped: list[Tool] = []
    for tool in tools:
        if isinstance(tool, WorkspaceScopedTool):
            scoped.append(tool)
            continue
        if tool.name in _FILE_TOOL_NAMES:
            scoped.append(WorkspaceScopedFileTool(tool, root_provider))
        elif _declares_working_dir(tool):
            scoped.append(WorkspaceScopedShellTool(tool, root_provider))
        else:
            scoped.append(tool)
    return scoped
