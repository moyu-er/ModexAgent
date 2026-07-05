"""Tests for per-pool resource isolation (refactored schema).

Verifies:
- Per-pool LLM provider independence
- Per-pool MemorySystem directory isolation
- PoolConfig name / main_agent_name decoupling (Task 1.5)
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

_BOT_PROJECT = Path(__file__).parent.parent.parent.parent / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from modex_agent.ioc.configs.pool import PoolConfig
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.configs.agent import AgentConfig


class TestPoolConfigNameDecouple:
    def test_name_and_main_agent_name_fields(self):
        """Pool has explicit name + main_agent_name fields."""
        cfg = PoolConfig(
            name="main",
            main_agent_name="main",
            llm=LLMConfig(model="gpt-4", api_key="k"),
            agents=[AgentConfig(name="main", role="main")],
        )
        assert cfg.name == "main"
        assert cfg.main_agent_name == "main"

    def test_name_can_differ_from_main_agent_name(self):
        """Directory name (name) can differ from the main agent's name."""
        cfg = PoolConfig(
            name="coding",
            main_agent_name="coder",
            llm=LLMConfig(model="gpt-4", api_key="k"),
            agents=[AgentConfig(name="coder", role="main")],
        )
        assert cfg.name == "coding"
        assert cfg.main_agent_name == "coder"
        assert cfg.name != cfg.main_agent_name


class TestPerPoolLLMIsolation:
    def test_different_models_per_pool(self):
        """Pools can use different LLM models."""
        cfg_main = PoolConfig(
            name="main",
            main_agent_name="main",
            llm=LLMConfig(model="openai/gpt-4", api_key="key-main"),
            agents=[AgentConfig(name="main", role="main")],
        )
        cfg_coding = PoolConfig(
            name="coding",
            main_agent_name="coding",
            llm=LLMConfig(model="openai/MiniMax-M2.5", api_key="key-coding"),
            agents=[AgentConfig(name="coding", role="main")],
        )
        assert cfg_main.llm.model != cfg_coding.llm.model
        assert cfg_main.llm.api_key != cfg_coding.llm.api_key


class TestPerPoolMemoryIsolation:
    def test_memory_config_per_pool(self):
        """Each pool can have different memory config."""
        cfg_coding = PoolConfig(
            name="coding",
            main_agent_name="coding",
            llm=LLMConfig(model="test", api_key="k"),
            agents=[AgentConfig(name="coding", role="main")],
            memory=None,
        )
        assert cfg_coding.memory is None

    def test_memory_inline_config(self):
        """Memory config can be specified inline."""
        from modex_agent.ioc.configs.memory import MemoryConfig, ShortTermConfig
        cfg = PoolConfig(
            name="main",
            main_agent_name="main",
            llm=LLMConfig(model="test", api_key="k"),
            agents=[AgentConfig(name="main", role="main")],
            memory=MemoryConfig(
                short_term=ShortTermConfig(max_context_tokens=150000),
            ),
        )
        assert cfg.memory is not None
        assert cfg.memory.short_term.max_context_tokens == 150000


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


class TestPoolConfigExtraForbid:
    def test_unknown_fields_rejected(self):
        """extra='forbid' rejects removed fields like mcp/terminal/skills."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            PoolConfig(
                name="main",
                main_agent_name="main",
                llm=LLMConfig(model="gpt-4", api_key="k"),
                agents=[AgentConfig(name="main", role="main")],
                mcp={"enabled": True},  # type: ignore[arg-type]
            )
        with pytest.raises(ValidationError):
            PoolConfig(
                name="main",
                main_agent_name="main",
                llm=LLMConfig(model="gpt-4", api_key="k"),
                agents=[AgentConfig(name="main", role="main")],
                terminal={"storage_dir": "x"},  # type: ignore[arg-type]
            )
