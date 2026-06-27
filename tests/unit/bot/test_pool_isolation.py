"""Tests for per-pool resource isolation.

Verifies:
- Per-pool LLM provider independence
- Per-pool TerminalManager storage isolation
- Per-pool MemorySystem directory isolation
- PoolConfig validation (filename/main_agent_name consistency)
- MCP config per-pool
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

_BOT_PROJECT = Path(__file__).parent.parent.parent.parent / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from modex_agent.ioc.configs.pool import PoolConfig, TerminalConfig
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.configs.agent import AgentConfig
from modex_agent.ioc.configs.mcp import MCPConfig


class TestPoolConfigValidation:
    def test_main_agent_name_from_role(self):
        """Pool identity = name of agent with role='main'."""
        cfg = PoolConfig(
            llm=LLMConfig(model="gpt-4", api_key="k"),
            agents=[
                AgentConfig(name="my-pool", role="main"),
                AgentConfig(name="helper", role="subagent"),
            ],
        )
        assert cfg.main_agent_name == "my-pool"

    def test_no_main_agent_raises(self):
        """Pool must have at least one role='main' agent."""
        cfg = PoolConfig(
            llm=LLMConfig(model="gpt-4", api_key="k"),
            agents=[
                AgentConfig(name="helper", role="subagent"),
            ],
        )
        with pytest.raises(ValueError, match="Pool must have exactly one agent"):
            _ = cfg.main_agent_name



class TestPerPoolTerminalIsolation:
    def test_different_storage_dirs(self):
        """Each pool has its own terminal storage_dir for shell session isolation."""
        cfg_main = PoolConfig(
            llm=LLMConfig(model="test", api_key="k"),
            agents=[AgentConfig(name="main", role="main", use_terminal=True)],
            terminal=TerminalConfig(storage_dir="data/terminals/main"),
        )
        cfg_coding = PoolConfig(
            llm=LLMConfig(model="test", api_key="k"),
            agents=[AgentConfig(name="coding", role="main", use_terminal=True)],
            terminal=TerminalConfig(storage_dir="data/terminals/coding"),
        )
        assert cfg_main.terminal.storage_dir != cfg_coding.terminal.storage_dir
        assert cfg_main.terminal.storage_dir == "data/terminals/main"
        assert cfg_coding.terminal.storage_dir == "data/terminals/coding"

    def test_terminal_disabled_no_storage(self):
        """If no agent uses terminal, storage_dir is still set but unused."""
        cfg = PoolConfig(
            llm=LLMConfig(model="test", api_key="k"),
            agents=[AgentConfig(name="main", role="main")],
        )
        assert cfg.terminal.storage_dir == "data/terminals"  # default


class TestPerPoolLLMIsolation:
    def test_different_models_per_pool(self):
        """Pools can use different LLM models."""
        cfg_main = PoolConfig(
            llm=LLMConfig(model="openai/gpt-4", api_key="key-main"),
            agents=[AgentConfig(name="main", role="main")],
        )
        cfg_coding = PoolConfig(
            llm=LLMConfig(model="openai/MiniMax-M2.5", api_key="key-coding"),
            agents=[AgentConfig(name="coding", role="main")],
        )
        assert cfg_main.llm.model != cfg_coding.llm.model
        assert cfg_main.llm.api_key != cfg_coding.llm.api_key

    def test_same_env_credentials_different_models(self):
        """Pools can share credentials from .env but use different models."""
        cfg_main = PoolConfig(
            llm=LLMConfig(model="openai/gpt-4", api_key="${LLM_API_KEY}"),
            agents=[AgentConfig(name="main", role="main")],
        )
        cfg_coding = PoolConfig(
            llm=LLMConfig(model="openai/claude-sonnet", api_key="${LLM_API_KEY}"),
            agents=[AgentConfig(name="coding", role="main")],
        )
        assert cfg_main.llm.api_key == cfg_coding.llm.api_key  # same env var
        assert cfg_main.llm.model != cfg_coding.llm.model


class TestPerPoolMcpIsolation:
    def test_mcp_config_dir_default(self):
        """Each pool shares the same MCP config directory convention."""
        cfg_main = PoolConfig(
            llm=LLMConfig(model="test", api_key="k"),
            agents=[AgentConfig(name="main", role="main")],
            mcp=MCPConfig(enabled=True),
        )
        cfg_coding = PoolConfig(
            llm=LLMConfig(model="test", api_key="k"),
            agents=[AgentConfig(name="coding", role="main")],
            mcp=MCPConfig(enabled=True),
        )
        # Both use the same config_dir, but agents load different files
        assert cfg_main.mcp.config_dir == cfg_coding.mcp.config_dir

    def test_mcp_can_be_none(self):
        """Pool can have no MCP config."""
        cfg = PoolConfig(
            llm=LLMConfig(model="test", api_key="k"),
            agents=[AgentConfig(name="main", role="main")],
        )
        assert cfg.mcp is None


class TestPerPoolMemoryIsolation:
    def test_memory_config_per_pool(self):
        """Each pool can have different memory config."""
        cfg_coding = PoolConfig(
            llm=LLMConfig(model="test", api_key="k"),
            agents=[AgentConfig(name="coding", role="main")],
            memory=None,
        )
        # Memory is optional — pool uses framework defaults when None
        assert cfg_coding.memory is None

    def test_memory_inline_config(self):
        """Memory config can be specified inline."""
        from modex_agent.ioc.configs.memory import MemoryConfig, ShortTermConfig
        cfg = PoolConfig(
            llm=LLMConfig(model="test", api_key="k"),
            agents=[AgentConfig(name="main", role="main")],
            memory=MemoryConfig(
                short_term=ShortTermConfig(max_tokens=150000),
            ),
        )
        assert cfg.memory is not None
        assert cfg.memory.short_term.max_tokens == 150000


class TestPoolInstanceStructure:
    def test_pool_instance_main_address(self):
        """PoolInstance.main_address returns correct AgentAddress."""
        from bot.service.pool_instance import PoolInstance
        pi = PoolInstance(
            name="coding",
            config=None,
            pool=None,
            broker_bridge=None,
            tool_manager=None,
            skill_manager=None,
            mcp_manager=None,
            terminal_manager=None,
            main_agent_name="coding",
            provider=None,
            notification_service=None,
            communication_service=None,
        )
        addr = pi.main_address
        assert addr.kind == "agent"
        assert addr.name == "coding"
