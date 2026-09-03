"""Execution-mode pin tests for all production Tool subclasses (ADR-0048 D1, ticket 2).

One pin per labelled production tool class, plus the two inheritance-only
entries (``AciEditTool`` rides ``EditFileTool``'s EXCLUSIVE chain; ``MCPTool``
stays fail-closed EXCLUSIVE by classvar default with the instance-level
override entry point) and explicit delegation pins for all three workspace
wrapper classes. Class-level assertions throughout — the property resolves
``type(self)._default_execution_mode`` and no tool state is needed.

The production inventory contains 40 leaf classes plus 3 wrappers. The pin
suite has 44 test items: one for each production class plus the MCP override
entry point. Each wrapper item verifies both delegation directions.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from modex_agent.agents import GraphDeliverTool
from modex_agent.core.tool_manager import ExecutionMode, Tool
from modex_agent.memory.tools.scoped_edit import ScopedEditFileTool
from modex_agent.memory.tools.scoped_list import ScopedListTool
from modex_agent.memory.tools.scoped_read import ScopedReadFileTool
from modex_agent.memory.tools.scoped_write import ScopedWriteFileTool
from modex_agent.multi_agent.tools import (
    SendToAgentTool,
    SendToPeerTool,
    TaskDispatchTool,
)
from modex_agent.plugins.defaults.capabilities.experience.catalog import (
    ExperienceDeleteTool,
    ExperienceEditTool,
    ExperienceListTool,
    ExperienceReadTool,
    ExperienceRenameDirTool,
    ExperienceRouterTool,
    ExperienceWriteTool,
)
from modex_agent.tools.aci.edit_tool import AciEditTool
from modex_agent.tools.ast.ast_replace import AstGrepReplaceTool
from modex_agent.tools.ast.ast_search import AstGrepSearchTool
from modex_agent.tools.graph_knowledge_tool import GraphKnowledgeBaseTool
from modex_agent.tools.lsp.lsp_diagnostics import LspDiagnosticsTool
from modex_agent.tools.lsp.lsp_navigation import LspNavigationTool
from modex_agent.tools.mcp.backend import McpBackend
from modex_agent.tools.mcp.tool import MCPTool
from modex_agent.tools.standard.file_tool import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from modex_agent.tools.standard.glob_tool import GlobTool
from modex_agent.tools.standard.search_tool import SearchFilesTool
from modex_agent.tools.standard.todo_tool import TodoReadTool, TodoWriteTool
from modex_agent.tools.terminal.command_tool import CommandTool
from modex_agent.tools.terminal.persistent_bash import (
    BashInputTool,
    PersistentBashTool,
)
from modex_agent.tools.terminal.process_tool import ProcessTool
from modex_agent.tools.terminal.subprocess_tool import SubprocessTool
from modex_agent.tools.terminal.tool import TerminalTool
from modex_agent.tools.web.reader import WebReaderTool
from modex_agent.tools.web.search import WebSearchTool
from modex_agent.tools.workspace_scoped import (
    WorkspaceRootProvider,
    WorkspaceScopedFileTool,
    WorkspaceScopedShellTool,
    WorkspaceScopedTool,
)

# examples/bot_project is not on the pytest pythonpath; add it the same way
# tests/unit/bot/test_pool_initialize.py does.
_BOT_PROJECT = Path(__file__).resolve().parents[3] / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from bot.tools.custom import SendFileToUserTool  # noqa: E402
from bot.tools.kb import KbTool  # noqa: E402


def _mode(cls: type[Tool]) -> ExecutionMode:
    """Resolve execution_mode without running a stateful ``__init__``.

    Every input the property reads (``_execution_mode_override``,
    ``_default_execution_mode``) is a class-level default, so a bare
    ``__new__`` instance exercises the real resolution path without
    constructing tool state.
    """
    instance = cls.__new__(cls)
    return instance.execution_mode  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# PARALLEL (17) — stateless reads and independent-session dispatches
# ---------------------------------------------------------------------------

_PARALLEL_TOOLS: list[type[Tool]] = [
    ReadFileTool,
    ListDirTool,
    SearchFilesTool,
    GlobTool,
    TodoReadTool,
    AstGrepSearchTool,
    LspNavigationTool,
    LspDiagnosticsTool,
    WebSearchTool,
    WebReaderTool,
    ScopedReadFileTool,
    ScopedListTool,
    ExperienceReadTool,
    ExperienceListTool,
    SendToAgentTool,
    TaskDispatchTool,
    SendToPeerTool,
]


# ---------------------------------------------------------------------------
# EXCLUSIVE (23 named) — writes, edits, every bash form, mixed read/write
# routers (kb / unified experience — fail-closed per the plan's errata)
# ---------------------------------------------------------------------------

_EXCLUSIVE_TOOLS: list[type[Tool]] = [
    WriteFileTool,
    EditFileTool,
    TodoWriteTool,
    PersistentBashTool,
    BashInputTool,
    CommandTool,
    SubprocessTool,
    ProcessTool,
    TerminalTool,
    AstGrepReplaceTool,
    GraphKnowledgeBaseTool,
    GraphDeliverTool,
    ScopedWriteFileTool,
    ScopedEditFileTool,
    ExperienceWriteTool,
    ExperienceEditTool,
    ExperienceRenameDirTool,
    ExperienceDeleteTool,
    ExperienceRouterTool,
    KbTool,
    SendFileToUserTool,
]


@pytest.mark.parametrize(
    "tool_cls", _PARALLEL_TOOLS, ids=[c.__name__ for c in _PARALLEL_TOOLS]
)
def test_parallel_tool_pin(tool_cls: type[Tool]) -> None:
    """Every stateless-read tool is labelled PARALLEL (ADR-0048 D1)."""
    assert _mode(tool_cls) is ExecutionMode.PARALLEL


@pytest.mark.parametrize(
    "tool_cls", _EXCLUSIVE_TOOLS, ids=[c.__name__ for c in _EXCLUSIVE_TOOLS]
)
def test_exclusive_tool_pin(tool_cls: type[Tool]) -> None:
    """Every write/edit/bash/mixed tool is labelled EXCLUSIVE (ADR-0048 D1)."""
    assert _mode(tool_cls) is ExecutionMode.EXCLUSIVE


# ---------------------------------------------------------------------------
# Inheritance-only entries (no declaration change needed)
# ---------------------------------------------------------------------------


def test_aci_edit_rides_exclusive_chain() -> None:
    """AciEditTool(EditFileTool) inherits EXCLUSIVE — no own marker needed."""
    assert _mode(AciEditTool) is ExecutionMode.EXCLUSIVE
    assert issubclass(AciEditTool, EditFileTool)


class _NullBackend(McpBackend):
    @property
    def connected_servers(self) -> list[str]:
        return []

    def _client_for(self, name: str) -> Any:
        return None

    async def release(self) -> None:
        pass


def _mcp_tool(execution_mode: ExecutionMode | None = None) -> MCPTool:
    return MCPTool(
        server_name="s1",
        tool_name="echo",
        description="echo",
        parameters={"type": "object", "properties": {}, "required": []},
        mcp_manager=_NullBackend(),
        execution_mode=execution_mode,
    )


def test_mcp_tool_default_exclusive() -> None:
    """MCPTool stays fail-closed EXCLUSIVE (classvar default, no marker)."""
    assert _mcp_tool().execution_mode is ExecutionMode.EXCLUSIVE


def test_mcp_tool_override_entry_point() -> None:
    """The ctor param is the per-server labelling entry point (adapter-side)."""
    assert _mcp_tool(ExecutionMode.PARALLEL).execution_mode is ExecutionMode.PARALLEL


# ---------------------------------------------------------------------------
# Wrapper delegation (3 wrapper classes, 2 directional pins)
# ---------------------------------------------------------------------------


class _PinnedRoot(WorkspaceRootProvider):
    def __init__(self, path: str = "/ws") -> None:
        self._path = path

    def current(self) -> Path:
        return Path(self._path)


class _StubParallel(ReadFileTool):
    """Concrete PARALLEL inner — no state needed for the property read."""


class _StubExclusive(WriteFileTool):
    """Concrete EXCLUSIVE inner — no state needed for the property read."""


def test_workspace_scoped_tool_delegates_both_inner_modes() -> None:
    parallel_wrapper = WorkspaceScopedTool(_StubParallel(), _PinnedRoot())
    exclusive_wrapper = WorkspaceScopedTool(_StubExclusive(), _PinnedRoot())

    assert parallel_wrapper.execution_mode is ExecutionMode.PARALLEL
    assert exclusive_wrapper.execution_mode is ExecutionMode.EXCLUSIVE


def test_workspace_scoped_file_tool_delegates_both_inner_modes() -> None:
    parallel_wrapper = WorkspaceScopedFileTool(_StubParallel(), _PinnedRoot())
    exclusive_wrapper = WorkspaceScopedFileTool(_StubExclusive(), _PinnedRoot())

    assert parallel_wrapper.execution_mode is ExecutionMode.PARALLEL
    assert exclusive_wrapper.execution_mode is ExecutionMode.EXCLUSIVE


def test_workspace_scoped_shell_tool_delegates_both_inner_modes() -> None:
    parallel_wrapper = WorkspaceScopedShellTool(_StubParallel(), _PinnedRoot())
    exclusive_wrapper = WorkspaceScopedShellTool(_StubExclusive(), _PinnedRoot())

    assert parallel_wrapper.execution_mode is ExecutionMode.PARALLEL
    assert exclusive_wrapper.execution_mode is ExecutionMode.EXCLUSIVE
