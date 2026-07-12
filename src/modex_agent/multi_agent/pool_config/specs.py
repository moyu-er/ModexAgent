"""Pool disk specs — renamed framework equivalents of pool_payloads models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.ioc.configs.approval import ApprovalConfig
from modex_agent.tools.presets import (
    DEFAULT_FORK_MAX_MESSAGES,
    MAX_FORK_MAX_MESSAGES,
    ContextMode,
    SystemPromptMode,
    ToolPreset,
    ToolSupplement,
)


class MainAgentSpec(BaseModel):
    """Editable main-agent disk projection. Rename of MainAgentNode."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_name: str
    description: str = ""
    max_steps: int = 100
    use_terminal: bool = False
    terminal_visibility: bool = False
    tool_preset: ToolPreset = ToolPreset.FULL
    tool_supplements: list[ToolSupplement] = Field(default_factory=lambda: [ToolSupplement.TODO])
    approval: ApprovalConfig | None = None
    mcp: list[str] = Field(default_factory=list)


class SubagentSpec(BaseModel):
    """Editable subagent disk projection. Rename of SubagentNode.

    NO approval, NO experience — subagents never have these capabilities.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_name: str
    description: str = ""
    max_steps: int = 80
    tool_preset: ToolPreset = ToolPreset.READ_WRITE
    tool_supplements: list[ToolSupplement] = Field(default_factory=list)
    context_mode: ContextMode = ContextMode.FRESH
    mcp: list[str] = Field(default_factory=list)
    system_prompt_mode: SystemPromptMode = SystemPromptMode.REPLACE
    fork_max_messages: int = Field(
        default=DEFAULT_FORK_MAX_MESSAGES, ge=1, le=MAX_FORK_MAX_MESSAGES
    )


class PoolSpec(BaseModel):
    """One pool's full disk projection. Rename of PoolTree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    main_agent_name: str
    main: MainAgentSpec
    subagents: list[SubagentSpec] = Field(default_factory=list)
    peers: list[str] = Field(default_factory=list)
    restart_required: bool = False
