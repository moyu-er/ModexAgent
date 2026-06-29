"""PoolConfig — configuration for one agent pool (system)."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from modex_agent.ioc.configs.agent import AgentConfig
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.configs.mcp import MCPConfig
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.ioc.configs.skills import SkillsConfig

# ---------------------------------------------------------------------------
# Byte-unit constant — single source of truth.
# ---------------------------------------------------------------------------
_MB: int = 1024 * 1024
_GB: int = 1024 * _MB


@dataclass(frozen=True)
class MediaConfig:
    """Attachment perception-gate + storage-budget configuration.

    Carries the single source of truth for the size caps and the per-session
    budget shared by upload-accept, path-injection, and inline-render
    (ADR-0013 §7). The dangerous-executable deny-list is a fixed security
    policy owned by :mod:`modex_agent.media.security`, not a tunable field
    here — a caller must not be able to disable disguise-rejection.

    Frozen value object — overrides are a new instance, not in-place mutation.
    Defaults: image 20 MB, text/doc 10 MB, session budget 500 MB, outbound
    cap 1 GB.
    """

    max_image_bytes: int = 20 * _MB
    max_text_doc_bytes: int = 10 * _MB
    session_budget_bytes: int = 500 * _MB
    max_outbound_bytes: int = _GB


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
    media: MediaConfig = Field(default_factory=MediaConfig)

    @property
    def main_agent_name(self) -> str:
        for a in self.agents:
            if a.role == "main":
                return a.name
        raise ValueError("Pool must have exactly one agent with role='main'")
