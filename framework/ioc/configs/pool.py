"""PoolConfig — configuration for one agent pool (system)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from framework.ioc.configs.agent import AgentConfig
from framework.ioc.configs.llm import LLMConfig
from framework.ioc.configs.mcp import MCPConfig
from framework.ioc.configs.memory import MemoryConfig
from framework.ioc.configs.skills import SkillsConfig


class TerminalConfig(BaseModel):
    """Per-pool terminal settings."""

    storage_dir: str = "data/terminals"
    max_terminals: int = 5


class PoolConfig(BaseModel):
    """Configuration for one agent pool.

    Pool identity = name of the agent with role='main'.
    """

    model_config = {"extra": "ignore"}

    llm: LLMConfig
    agents: list[AgentConfig] = Field(default_factory=list)
    mcp: MCPConfig | None = None
    memory: MemoryConfig | None = None
    skills: SkillsConfig | None = None
    terminal: TerminalConfig = Field(default_factory=TerminalConfig)

    @property
    def main_agent_name(self) -> str:
        for a in self.agents:
            if a.role == "main":
                return a.name
        raise ValueError("Pool must have exactly one agent with role='main'")
