"""Tests for framework.ioc.factories.descriptors."""

import asyncio
from pathlib import Path

import pytest

from modex_agent.ioc.configs.agent import AgentConfig
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.configs.skills import SkillsConfig
from modex_agent.ioc.factories.descriptors import build_subagent_descriptor


class TestBuildSubagentDescriptorQuery12306:
    @pytest.mark.anyio
    async def test_query_12306_standard_tools(self) -> None:
        app_cfg = AppConfig(llm=LLMConfig())
        agent_cfg = AgentConfig(name="query-12306")
        project_dir = Path("/tmp")
        workspace = Path("/tmp/memory")

        desc, tm, sm, mem = await build_subagent_descriptor(
            agent_cfg, app_cfg, project_dir, workspace,
            safety=None, llm=None,
        )
        assert desc.address.name == "query-12306"
        # Standard tools always registered (read_write default)
        assert "read" in tm.list_tools()
        assert "write" in tm.list_tools()
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
        assert "read" in tools
        assert "write" in tools
        assert "bash" in tools
        assert "grep" in tools


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
        assert "read" in tools
        assert "spawn_subagent" not in tools  # denied
        assert "send_message" not in tools  # denied
        assert desc.context_strategy == "persistent"
