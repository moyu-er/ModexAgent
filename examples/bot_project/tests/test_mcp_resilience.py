from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bot.service.react_strategy import ReactExecutionStrategy

from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.pool_config.specs import MainAgentSpec


class TestBuildToolsMcpResilience:
    @pytest.fixture
    def main_spec(self) -> MainAgentSpec:
        return MainAgentSpec(agent_name="main")

    @pytest.fixture
    def assembly_deps(self) -> PoolAssemblyDeps:
        return PoolAssemblyDeps()

    @pytest.fixture
    def project_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent

    @pytest.fixture
    def data_dir(self) -> Path:
        with TemporaryDirectory() as tmp:
            return Path(tmp)

    @pytest.fixture
    def strategy(self) -> ReactExecutionStrategy:
        return ReactExecutionStrategy()

    @pytest.mark.asyncio
    async def test_mcp_empty_selection_skips_loading(
        self,
        main_spec: MainAgentSpec,
        assembly_deps: PoolAssemblyDeps,
        project_dir: Path,
        data_dir: Path,
        strategy: ReactExecutionStrategy,
    ) -> None:
        assert main_spec.mcp == []

        output_adapter = MagicMock()

        with patch(
            "bot.service.builders._load_agent_mcp_tools",
            new=AsyncMock(return_value=([], None)),
        ) as mock_load_mcp:
            tool_manager, mcp_manager, _todo_store = await strategy._build_tools(
                main_spec=main_spec,
                assembly_deps=assembly_deps,
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
        assembly_deps: PoolAssemblyDeps,
        project_dir: Path,
        data_dir: Path,
        strategy: ReactExecutionStrategy,
    ) -> None:
        main_spec = MainAgentSpec(agent_name="main", mcp=["playwright"])

        output_adapter = MagicMock()

        with patch(
            "bot.service.builders._load_agent_mcp_tools",
            new=AsyncMock(side_effect=RuntimeError("MCP boom")),
        ) as mock_load_mcp:
            tool_manager, mcp_manager, _todo_store = await strategy._build_tools(
                main_spec=main_spec,
                assembly_deps=assembly_deps,
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
