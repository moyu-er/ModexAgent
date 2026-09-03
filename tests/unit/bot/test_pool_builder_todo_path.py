"""Pool todo wiring through the declaration compiler (tickets 05/11).

The todo store is the ``todo`` capability's pool-level supply
(``TodoCapability.supply`` — the T11 convergence; the retired BIZ
``build_pool_todo_store`` and the typed ``pool_runtime.todo_store`` /
``AgentMaterializeDeps.todo_store`` carriers died with it); the todo
TOOLS are declaration-resolved — ``capabilities: {todo: {}}`` contributes
``todo_write``/``todo_read`` into the compiled AssemblySpec's merge base
(the T10 migration), resolved through the TOOL-slot factories against
``capability_supply['todo']``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("aiohttp")  # transitive: ReactExecutionStrategy → web_ui_service → aiohttp

from modex_agent.plugins.capability import PoolSupplyAgentEntry, PoolSupplyView
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.defaults.capabilities.todo import TodoCapability, TodoSupply
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.runtime.todo import JsonFileTodoStore
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths


def _workspace_ctx(root: Path) -> WorkspaceContext:
    return WorkspaceContext(target=root, paths=WorkspacePaths(root=root / ".modex"), is_home=False)


def _registry() -> ComponentRegistry:
    """A registry carrying the FW defaults (the todo capability lives in
    DefaultPlugin — the production registration face)."""
    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    DefaultPlugin().register(ctx)
    ctx.flush()
    return registry


def _view(pool_name: str = "main", **kwargs: object) -> PoolSupplyView:
    return PoolSupplyView(
        pool_name=pool_name,
        entries=(PoolSupplyAgentEntry(agent_name="main", config={}),),
        **kwargs,  # type: ignore[arg-type]
    )


def test_todo_supply_uses_pool_runtime_dir_when_pool_data_present(
    tmp_path: Path,
) -> None:
    """Parity with the retired ``build_pool_todo_store``: the workspace's
    pool_data runtime dir wins when present."""
    runtime_dir = tmp_path / "ws" / "runtime_state" / "main"

    supply = TodoCapability().supply(_view(runtime_dir=runtime_dir, data_dir=tmp_path / "data"))

    assert isinstance(supply, TodoSupply)
    assert isinstance(supply.store, JsonFileTodoStore)
    assert supply.store._base_dir == runtime_dir / "todos"  # noqa: SLF001


def test_todo_supply_falls_back_to_data_dir_path_without_pool_data(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"

    supply = TodoCapability().supply(_view(data_dir=data_dir))

    assert isinstance(supply, TodoSupply)
    assert isinstance(supply.store, JsonFileTodoStore)
    assert supply.store._base_dir == data_dir / "runtime_state" / "main" / "todos"  # noqa: SLF001


def _compiled_tools(*agents: AgentSpec) -> dict[str, list[str]]:
    """Compile a single-pool declaration and return agent_name → tools."""
    spec = ScopeSpec(kind=ScopeKind.POOL, pool=PoolSpec(name="p", agents=list(agents)))
    compilation = compile_scope(spec, workspace_ctx=_workspace_ctx(Path(".")), registry=_registry())
    return {a.provenance.agent: a.spec.tools for a in compilation.agents}


def test_todo_capability_contributes_compiled_tool_names_for_main_agent() -> None:
    """A main agent declaring ``capabilities: {todo: {}}`` carries the todo
    tool names in its compiled spec — Stage 4 resolves them through
    TodoToolFactory against the pool store."""
    tools = _compiled_tools(AgentSpec(name="main", capabilities={"todo": {}}))

    assert "todo_write" in tools["main"]
    assert "todo_read" in tools["main"]


def test_subagent_todo_capability_contributes_compiled_tool_names() -> None:
    tools = _compiled_tools(
        AgentSpec(name="main"),
        AgentSpec(name="helper", parent="main", capabilities={"todo": {}}),
    )

    assert "todo_write" in tools["helper"]
    assert "todo_read" in tools["helper"]


def test_no_todo_capability_carries_no_todo_tool_names() -> None:
    tools = _compiled_tools(AgentSpec(name="main"))

    assert "todo_write" not in tools["main"]
    assert "todo_read" not in tools["main"]
