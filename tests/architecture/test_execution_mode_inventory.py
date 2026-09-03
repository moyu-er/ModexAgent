"""Architecture guard: execution-mode inventory of every production Tool.

Pins the full ADR-0048 D1 classification (with the plan's two errata:
``kb`` and the unified ``experience`` tool are EXCLUSIVE — fail-closed,
multi-action mixed read/write). Every production tool class named in the
ticket-2 inventory must be importable from its source module and resolve
the expected execution mode; a missing class (deleted/moved without
updating this inventory) fails the guard just like a wrong label.

Runtime inventory check: no registration assembly is built — the guard imports
the classes directly from their source modules and walks the complete ``Tool``
subclass closure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from modex_agent.core.tool_manager import ExclusiveTool, ExecutionMode, ParallelTool, Tool

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BOT_PROJECT = _REPO_ROOT / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from bot.tools.custom import SendFileToUserTool  # noqa: E402
from bot.tools.kb import KbTool  # noqa: E402

from modex_agent.memory.tools.scoped_edit import ScopedEditFileTool  # noqa: E402
from modex_agent.memory.tools.scoped_list import ScopedListTool  # noqa: E402
from modex_agent.memory.tools.scoped_read import ScopedReadFileTool  # noqa: E402
from modex_agent.memory.tools.scoped_write import ScopedWriteFileTool  # noqa: E402
from modex_agent.multi_agent.tools import (  # noqa: E402
    SendToAgentTool,
    SendToPeerTool,
    TaskDispatchTool,
)
from modex_agent.plugins.defaults.capabilities.experience.catalog import (  # noqa: E402
    ExperienceDeleteTool,
    ExperienceEditTool,
    ExperienceListTool,
    ExperienceReadTool,
    ExperienceRenameDirTool,
    ExperienceRouterTool,
    ExperienceWriteTool,
)
from modex_agent.tools.aci.edit_tool import AciEditTool  # noqa: E402
from modex_agent.tools.ast.ast_replace import AstGrepReplaceTool  # noqa: E402
from modex_agent.tools.ast.ast_search import AstGrepSearchTool  # noqa: E402
from modex_agent.tools.graph_deliver import GraphDeliverTool  # noqa: E402
from modex_agent.tools.graph_knowledge_tool import GraphKnowledgeBaseTool  # noqa: E402
from modex_agent.tools.lsp.lsp_diagnostics import LspDiagnosticsTool  # noqa: E402
from modex_agent.tools.lsp.lsp_navigation import LspNavigationTool  # noqa: E402
from modex_agent.tools.mcp.tool import MCPTool  # noqa: E402
from modex_agent.tools.standard.file_tool import (  # noqa: E402
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from modex_agent.tools.standard.glob_tool import GlobTool  # noqa: E402
from modex_agent.tools.standard.search_tool import SearchFilesTool  # noqa: E402
from modex_agent.tools.standard.todo_tool import TodoReadTool, TodoWriteTool  # noqa: E402
from modex_agent.tools.terminal.command_tool import CommandTool  # noqa: E402
from modex_agent.tools.terminal.persistent_bash import (  # noqa: E402
    BashInputTool,
    PersistentBashTool,
)
from modex_agent.tools.terminal.process_tool import ProcessTool  # noqa: E402
from modex_agent.tools.terminal.subprocess_tool import SubprocessTool  # noqa: E402
from modex_agent.tools.terminal.tool import TerminalTool  # noqa: E402
from modex_agent.tools.web.reader import WebReaderTool  # noqa: E402
from modex_agent.tools.web.search import WebSearchTool  # noqa: E402
from modex_agent.tools.workspace_scoped import (  # noqa: E402
    WorkspaceScopedFileTool,
    WorkspaceScopedShellTool,
    WorkspaceScopedTool,
)

EXPECTED_MODES: dict[str, tuple[type[Tool], ExecutionMode]] = {
    # -- PARALLEL: stateless reads and independent-session dispatches -----
    "read": (ReadFileTool, ExecutionMode.PARALLEL),
    "ls": (ListDirTool, ExecutionMode.PARALLEL),
    "grep": (SearchFilesTool, ExecutionMode.PARALLEL),
    "glob": (GlobTool, ExecutionMode.PARALLEL),
    "todo_read": (TodoReadTool, ExecutionMode.PARALLEL),
    "ast_grep_search": (AstGrepSearchTool, ExecutionMode.PARALLEL),
    "lsp_navigation": (LspNavigationTool, ExecutionMode.PARALLEL),
    "lsp_diagnostics": (LspDiagnosticsTool, ExecutionMode.PARALLEL),
    "web_search": (WebSearchTool, ExecutionMode.PARALLEL),
    "web_reader": (WebReaderTool, ExecutionMode.PARALLEL),
    "scoped_read": (ScopedReadFileTool, ExecutionMode.PARALLEL),
    "scoped_list": (ScopedListTool, ExecutionMode.PARALLEL),
    "experience_read": (ExperienceReadTool, ExecutionMode.PARALLEL),
    "experience_list": (ExperienceListTool, ExecutionMode.PARALLEL),
    "send_to_agent": (SendToAgentTool, ExecutionMode.PARALLEL),
    "task": (TaskDispatchTool, ExecutionMode.PARALLEL),
    "send_to_peer": (SendToPeerTool, ExecutionMode.PARALLEL),
    # -- EXCLUSIVE: writes, edits, every bash form, mixed routers ----------
    "write": (WriteFileTool, ExecutionMode.EXCLUSIVE),
    "edit": (EditFileTool, ExecutionMode.EXCLUSIVE),
    "todo_write": (TodoWriteTool, ExecutionMode.EXCLUSIVE),
    "bash#persistent": (PersistentBashTool, ExecutionMode.EXCLUSIVE),
    "bash_input": (BashInputTool, ExecutionMode.EXCLUSIVE),
    "bash#command": (CommandTool, ExecutionMode.EXCLUSIVE),
    "bash#subprocess": (SubprocessTool, ExecutionMode.EXCLUSIVE),
    "process": (ProcessTool, ExecutionMode.EXCLUSIVE),
    "terminal": (TerminalTool, ExecutionMode.EXCLUSIVE),
    "ast_grep_replace": (AstGrepReplaceTool, ExecutionMode.EXCLUSIVE),
    "aci_edit": (AciEditTool, ExecutionMode.EXCLUSIVE),
    "knowledge_base": (GraphKnowledgeBaseTool, ExecutionMode.EXCLUSIVE),
    "deliver": (GraphDeliverTool, ExecutionMode.EXCLUSIVE),
    "scoped_write": (ScopedWriteFileTool, ExecutionMode.EXCLUSIVE),
    "scoped_edit": (ScopedEditFileTool, ExecutionMode.EXCLUSIVE),
    "experience_write": (ExperienceWriteTool, ExecutionMode.EXCLUSIVE),
    "experience_edit": (ExperienceEditTool, ExecutionMode.EXCLUSIVE),
    "experience_rename_dir": (ExperienceRenameDirTool, ExecutionMode.EXCLUSIVE),
    "experience_delete": (ExperienceDeleteTool, ExecutionMode.EXCLUSIVE),
    # Errata (plan ticket 2): unified multi-action routers are fail-closed
    "experience": (ExperienceRouterTool, ExecutionMode.EXCLUSIVE),
    "kb": (KbTool, ExecutionMode.EXCLUSIVE),
    "send_file_to_user": (SendFileToUserTool, ExecutionMode.EXCLUSIVE),
    # MCPTool: classvar default EXCLUSIVE; instance override is the adapter's
    # per-server entry point (not statically labelled)
    "mcp#default": (MCPTool, ExecutionMode.EXCLUSIVE),
}


def _resolve_mode(cls: type[Tool]) -> ExecutionMode:
    instance = cls.__new__(cls)
    return instance.execution_mode


def _qualified_class_name(cls: type[Any]) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _transitive_tool_subclasses(root: type[Any]) -> set[type[Any]]:
    subclasses: set[type[Any]] = set()
    pending = list(root.__subclasses__())
    while pending:
        subclass = pending.pop()
        if subclass in subclasses:
            continue
        subclasses.add(subclass)
        pending.extend(subclass.__subclasses__())
    return subclasses


def _inventory_class_names(classes: set[type[Any]]) -> set[str]:
    marker_classes = {ParallelTool, ExclusiveTool}
    return {
        _qualified_class_name(cls)
        for cls in classes
        if cls not in marker_classes
        and not cls.__name__.startswith("_")
        and cls.__module__.startswith(("modex_agent.", "bot."))
    }


@pytest.mark.parametrize("name", sorted(EXPECTED_MODES), ids=sorted(EXPECTED_MODES))
def test_production_tool_matches_expected_mode(name: str) -> None:
    """Every inventory entry resolves its declared mode; drift fails by name."""
    cls, expected = EXPECTED_MODES[name]
    assert _resolve_mode(cls) is expected, (
        f"{name} ({cls.__module__}.{cls.__qualname__}) drifted: "
        f"expected {expected.value}"
    )


def test_inventory_complete_no_extra_production_tool_classes() -> None:
    """No production Tool subclass outside the inventory escapes labelling.

    Walks the transitive runtime subclass closure from ``Tool`` and compares
    exact fully-qualified class names. This catches marker-derived tools,
    wrapper subclasses, and extra classes in modules already represented by
    the inventory.
    """
    expected_classes = {
        cls for cls, _ in EXPECTED_MODES.values()
    } | {
        WorkspaceScopedTool,
        WorkspaceScopedFileTool,
        WorkspaceScopedShellTool,
    }
    expected_names = {_qualified_class_name(cls) for cls in expected_classes}
    subclasses = _transitive_tool_subclasses(Tool)
    actual_names = _inventory_class_names(subclasses)
    missing = sorted(expected_names - actual_names)
    unknown = sorted(actual_names - expected_names)

    assert not missing and not unknown, (
        "Execution-mode inventory differs from production Tool subclasses: "
        f"missing={missing}, unknown={unknown}"
    )


def test_tool_subclass_collection_is_transitive() -> None:
    class _DirectTool(Tool):
        async def execute(self, **kwargs: Any) -> None:
            return None

    class _IndirectTool(_DirectTool):
        pass

    assert _IndirectTool in _transitive_tool_subclasses(Tool)


def test_inventory_completeness_compares_fully_qualified_class_names() -> None:
    class UnlistedClassInKnownModule:
        __module__ = ReadFileTool.__module__

    expected = {_qualified_class_name(ReadFileTool)}
    actual = _inventory_class_names({ReadFileTool, UnlistedClassInKnownModule})

    assert sorted(actual - expected) == [
        _qualified_class_name(UnlistedClassInKnownModule)
    ]
