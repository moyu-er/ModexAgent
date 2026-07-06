"""Tests for MCP resilience in pool building.

Ensures MCP selection resolution / failures do not block the main
tool-manager / pool creation flow.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.service.pool_builder import _build_tools
from modex_agent.ioc.configs.agent import AgentConfig
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.ioc.configs.pool import PoolConfig


def _pool_cfg() -> PoolConfig:
    return PoolConfig(
        name="main",
        main_agent_name="main",
        llm=LLMConfig(model="openai/gpt-4", api_key="sk-xxx"),
        agents=[AgentConfig(name="main", role="main")],
        memory=MemoryConfig(),
    )


class TestBuildToolsMcpResilience:
    @pytest.fixture
    def minimal_pool_cfg(self) -> PoolConfig:
        return _pool_cfg()

    @pytest.fixture
    def minimal_main_cfg(self) -> AgentConfig:
        return AgentConfig(name="main", role="main")

    @pytest.fixture
    def project_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent

    @pytest.fixture
    def data_dir(self) -> Path:
        with TemporaryDirectory() as tmp:
            return Path(tmp)

    @pytest.mark.asyncio
    async def test_mcp_empty_selection_skips_loading(
        self,
        minimal_pool_cfg: PoolConfig,
        minimal_main_cfg: AgentConfig,
        project_dir: Path,
        data_dir: Path,
    ) -> None:
        """Empty mcp selection → no MCP tools loaded."""
        # main_cfg.mcp defaults to []
        assert minimal_main_cfg.mcp == []

        output_adapter = MagicMock()

        with patch(
            "bot.service.pool_builder._load_agent_mcp_tools",
            new=AsyncMock(return_value=([], None)),
        ) as mock_load_mcp:
            tool_manager, mcp_manager, _todo_store = await _build_tools(
                pool_cfg=minimal_pool_cfg,
                main_cfg=minimal_main_cfg,
                terminal_manager=None,
                project_dir=project_dir,
                output_adapter=output_adapter,
                pool_name="main",
                data_dir=data_dir,
                pool_data=None,
                root_provider=None,
            )

        mock_load_mcp.assert_not_called()
        assert mcp_manager is None
        tool_names = tool_manager.list_tools()
        assert not any(name.startswith("mcp_") for name in tool_names)

    @pytest.mark.asyncio
    async def test_mcp_failure_does_not_block_tool_manager(
        self,
        minimal_pool_cfg: PoolConfig,
        project_dir: Path,
        data_dir: Path,
    ) -> None:
        """When MCP loading raises, the tool manager is still built."""
        main_cfg = AgentConfig(name="main", role="main", mcp=["playwright"])

        output_adapter = MagicMock()

        with patch(
            "bot.service.pool_builder._load_agent_mcp_tools",
            new=AsyncMock(side_effect=RuntimeError("MCP boom")),
        ) as mock_load_mcp:
            tool_manager, mcp_manager, _todo_store = await _build_tools(
                pool_cfg=minimal_pool_cfg,
                main_cfg=main_cfg,
                terminal_manager=None,
                project_dir=project_dir,
                output_adapter=output_adapter,
                pool_name="main",
                data_dir=data_dir,
                pool_data=None,
                root_provider=None,
            )

        mock_load_mcp.assert_called_once()
        assert mcp_manager is None
        tool_names = tool_manager.list_tools()
        assert len(tool_names) > 0
