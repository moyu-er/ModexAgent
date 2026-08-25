"""Pool todo wiring through the declaration compiler (tickets 05/11).

The todo store is pool-scoped supplied infra (``build_pool_todo_store`` —
pool-aware path + backend selection, handed to TodoToolFactory via
``pool_runtime.todo_store``); the todo TOOLS are declaration-resolved —
``tool_supplements: [todo]`` expands to ``todo_write``/``todo_read`` in the
compiled AssemblySpec, resolved through the TOOL-slot factories.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("aiohttp")  # transitive: ReactExecutionStrategy → web_ui_service → aiohttp

from bot.service.builders import build_pool_todo_store

from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.runtime.store import JsonFileTodoStore
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.tools.presets import ToolSupplement
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths


def _snapshot_with_runtime_dir(runtime_dir: Path) -> PoolDataSnapshot:
    return PoolDataSnapshot(
        context_manager=None,  # type: ignore[arg-type]
        turn_store=None,  # type: ignore[arg-type]
        trace_store=None,
        memory_dir=None,
        runtime_dir=runtime_dir,
        pruned_manager=None,
        experience_dir=None,
    )


def _workspace_ctx(root: Path) -> WorkspaceContext:
    return WorkspaceContext(
        target=root, paths=WorkspacePaths(root=root / ".modex"), is_home=False
    )


def test_todo_store_uses_pool_runtime_dir_when_pool_data_present(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "ws" / "runtime_state" / "main"
    pool_data = _snapshot_with_runtime_dir(runtime_dir)

    store = build_pool_todo_store(None, None, pool_data, "main", tmp_path / "data")

    assert isinstance(store, JsonFileTodoStore)
    assert store._base_dir == runtime_dir / "todos"  # noqa: SLF001


def test_todo_store_falls_back_to_data_dir_path_without_pool_data(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"

    store = build_pool_todo_store(None, None, None, "main", data_dir)

    assert isinstance(store, JsonFileTodoStore)
    assert store._base_dir == data_dir / "runtime_state" / "main" / "todos"  # noqa: SLF001



def _compiled_tools(*agents: AgentSpec) -> dict[str, list[str]]:
    """Compile a single-pool declaration and return agent_name → tools."""
    spec = ScopeSpec(kind=ScopeKind.POOL, pool=PoolSpec(name="p", agents=list(agents)))
    compilation = compile_scope(spec, workspace_ctx=_workspace_ctx(Path(".")))
    return {a.provenance.agent: a.spec.tools for a in compilation.agents}


def test_todo_supplement_expands_to_compiled_tool_names_for_main_agent() -> None:
    """A main agent declaring ``tool_supplements: [todo]`` carries the todo
    tool names in its compiled spec — Stage 4 resolves them through
    TodoToolFactory against the pool store."""
    tools = _compiled_tools(AgentSpec(name="main", tool_supplements=[ToolSupplement.TODO]))

    assert "todo_write" in tools["main"]
    assert "todo_read" in tools["main"]


def test_subagent_todo_supplement_expands_to_compiled_tool_names() -> None:
    tools = _compiled_tools(
        AgentSpec(name="main"),
        AgentSpec(name="helper", parent="main", tool_supplements=[ToolSupplement.TODO]),
    )

    assert "todo_write" in tools["helper"]
    assert "todo_read" in tools["helper"]


def test_no_todo_supplement_carries_no_todo_tool_names() -> None:
    tools = _compiled_tools(AgentSpec(name="main"))

    assert "todo_write" not in tools["main"]
    assert "todo_read" not in tools["main"]
