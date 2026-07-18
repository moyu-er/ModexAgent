"""Behavioral tests for the pool builder todo wiring.

These verify that the todo store is created with the correct pool-aware path
and that todo tools are registered when the main agent's tool_supplements
includes the todo supplement.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from bot.service.react_strategy import ReactExecutionStrategy

from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.pool_config.specs import MainAgentSpec
from modex_agent.runtime.store import JsonFileTodoStore
from modex_agent.tools.presets import ToolSupplement


@pytest.mark.asyncio
async def test_build_tools_registers_todo_tools_and_pool_todo_store(
    tmp_path: Path,
) -> None:
    pool_name = "main"
    data_dir = tmp_path / "data"
    expected_todo_dir = data_dir / "runtime_state" / pool_name / "todos"

    main_spec = MainAgentSpec(
        agent_name="main",
        tool_supplements=[ToolSupplement.TODO],
    )
    assembly_deps = PoolAssemblyDeps()
    strategy = ReactExecutionStrategy()

    tool_manager, _mcp_manager, todo_store = await strategy._build_tools(
        main_spec=main_spec,
        assembly_deps=assembly_deps,
        terminal_manager=None,
        project_dir=tmp_path,
        output_adapter=AsyncMock(),
        pool_name=pool_name,
        data_dir=data_dir,
        pool_data=None,
        root_provider=None,
    )

    assert isinstance(todo_store, JsonFileTodoStore)
    assert todo_store._base_dir == expected_todo_dir
    assert tool_manager.is_registered("todo_read")
    assert tool_manager.is_registered("todo_write")


@pytest.mark.asyncio
async def test_build_tools_without_todo_supplement_does_not_register_todo_tools(
    tmp_path: Path,
) -> None:
    main_spec = MainAgentSpec(agent_name="main", tool_supplements=[])
    assembly_deps = PoolAssemblyDeps()
    strategy = ReactExecutionStrategy()

    tool_manager, _mcp_manager, _todo_store = await strategy._build_tools(
        main_spec=main_spec,
        assembly_deps=assembly_deps,
        terminal_manager=None,
        project_dir=tmp_path,
        output_adapter=AsyncMock(),
        pool_name="main",
        data_dir=tmp_path / "data",
        pool_data=None,
        root_provider=None,
    )

    assert not tool_manager.is_registered("todo_read")
    assert not tool_manager.is_registered("todo_write")
