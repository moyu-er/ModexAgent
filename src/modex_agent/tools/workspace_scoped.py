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

from modex_agent.core.tool_manager import ExecutionMode, Tool
from modex_agent.workspace.boundary import canonicalize_path

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

    @property
    def execution_mode(self) -> ExecutionMode:
        """Delegate to the inner tool — inner tools span both execution modes.

        A statically inherited marker would be wrong for one of them
        (read tools wrap PARALLEL, write tools EXCLUSIVE), so the wrapper
        reports whatever the inner tool resolves to (including the inner
        instance-level override).
        """
        return self._inner.execution_mode

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
      - all paths use the same canonical resolver as permission checks
    """

    _PATH_ARG = "path"

    def _scoped_args(self, arguments: dict[str, Any]) -> dict[str, Any]:
        # Never strip a nonempty path: doing so changes the target AFTER its
        # permission check (e.g. " ../outside" is not "../outside").
        raw = arguments.get(self._PATH_ARG)
        path = str(raw) if raw is not None else ""
        arguments[self._PATH_ARG] = str(canonicalize_path(
            path if path.strip() else ".",
            base=self._root_provider.current(),
        ))
        return arguments


class WorkspaceScopedShellTool(WorkspaceScopedTool):
    """Wraps ``SubprocessTool`` (``bash``), whose cwd argument is
    ``working_dir``.

    Missing cwd defaults to the live workspace. Explicit relative cwd uses
    that same root rather than the inner executor's assembly-time default.
    """

    _CWD_ARG = "working_dir"

    def _scoped_args(self, arguments: dict[str, Any]) -> dict[str, Any]:
        arguments[self._CWD_ARG] = str(canonicalize_path(
            arguments.get(self._CWD_ARG) or ".", base=self._root_provider.current(),
        ))
        return arguments


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
    from modex_agent.sandbox.tool_matrix import ToolEffect, describe_tool_security

    scoped: list[Tool] = []
    for tool in tools:
        if isinstance(tool, WorkspaceScopedTool):
            scoped.append(tool)
            continue
        descriptor = describe_tool_security(tool.name)
        if descriptor.effect in (ToolEffect.READ, ToolEffect.WRITE) and descriptor.target_argument == "path":
            scoped.append(WorkspaceScopedFileTool(tool, root_provider))
        elif _declares_working_dir(tool):
            scoped.append(WorkspaceScopedShellTool(tool, root_provider))
        else:
            scoped.append(tool)
    return scoped
