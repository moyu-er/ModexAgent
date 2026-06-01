"""Tests for framework.ioc.factories.descriptors."""

import asyncio
from pathlib import Path

import pytest

from framework.ioc.configs.agent import AgentConfig
from framework.ioc.configs.app import AppConfig
from framework.ioc.configs.llm import LLMConfig
from framework.ioc.configs.skills import SkillsConfig
from framework.ioc.factories.descriptors import build_subagent_descriptor


class TestBuildSubagentDescriptorQuery12306:
    @pytest.mark.anyio
    async def test_query_12306_mcp_only(self) -> None:
        app_cfg = AppConfig(llm=LLMConfig())
        agent_cfg = AgentConfig(name="query-12306", standard_tools=False)
        project_dir = Path("/tmp")
        workspace = Path("/tmp/memory")

        desc, tm, sm, mem = await build_subagent_descriptor(
            agent_cfg, app_cfg, project_dir, workspace,
            safety=None, llm=None,
        )
        assert desc.address.name == "query-12306"
        assert len(tm.list_tools()) == 0  # no standard tools, MCP added later
        assert sm is None

    @pytest.mark.anyio
    async def test_office_expert_standard_tools(self) -> None:
        app_cfg = AppConfig(llm=LLMConfig())
        agent_cfg = AgentConfig(
            name="office-expert",
            skills=SkillsConfig(roots=["skills/subagents/docx"]),
        )
        project_dir = Path("/tmp")
        workspace = Path("/tmp/memory")

        desc, tm, sm, mem = await build_subagent_descriptor(
            agent_cfg, app_cfg, project_dir, workspace,
            safety=None, llm=None,
        )
        tools = tm.list_tools()
        assert "read_file" in tools
        assert "write_file" in tools
        assert "bash" in tools
        assert "grep" in tools
        assert desc.max_tools_per_turn == 10


class TestBuildSubagentDescriptor:
    @pytest.mark.anyio
    async def test_standard_tools_denied_communication(self) -> None:
        app_cfg = AppConfig(llm=LLMConfig())
        agent_cfg = AgentConfig(name="helper-sync")
        project_dir = Path("/tmp")
        workspace = Path("/tmp/memory")

        desc, tm, sm, mem = await build_subagent_descriptor(
            agent_cfg, app_cfg, project_dir, workspace,
            safety=None, llm=None,
        )
        tools = tm.list_tools()
        assert "read_file" in tools
        assert "spawn_subagent" not in tools  # denied
        assert "send_message" not in tools  # denied
        assert desc.context_strategy == "persistent"
        assert desc.streaming_to_user is True
