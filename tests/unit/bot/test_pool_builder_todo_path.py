"""Behavioral tests for the pool builder todo wiring.

These verify that the todo store is created with the correct pool-aware path
and that todo tools are registered when the main agent's tool_supplements
includes the todo supplement.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from bot.service.pool_builder import _build_tools

from modex_agent.ioc.configs.agent import AgentConfig
from modex_agent.ioc.configs.pool import PoolConfig
from modex_agent.runtime.store import JsonFileTodoStore
from modex_agent.tools.presets import ToolSupplement


@pytest.mark.asyncio
async def test_build_tools_registers_todo_tools_and_pool_todo_store(
    tmp_path: Path,
) -> None:
    """When the main agent enables the todo supplement, the builder creates a
    pool-aware JsonFileTodoStore and registers both todo_read and todo_write.
    """
    pool_name = "main"
    data_dir = tmp_path / "data"
    expected_todo_dir = data_dir / "runtime_state" / pool_name / "todos"

    pool_cfg = PoolConfig(name=pool_name, main_agent_name="main")
    main_cfg = AgentConfig(
        name="main",
        role="main",
        tool_supplements=[ToolSupplement.TODO],
    )

    tool_manager, _mcp_manager, todo_store = await _build_tools(
        pool_cfg=pool_cfg,
        main_cfg=main_cfg,
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
    """A main agent with no todo supplement should not receive todo tools."""
    pool_cfg = PoolConfig(name="main", main_agent_name="main")
    main_cfg = AgentConfig(name="main", role="main", tool_supplements=[])

    tool_manager, _mcp_manager, _todo_store = await _build_tools(
        pool_cfg=pool_cfg,
        main_cfg=main_cfg,
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
