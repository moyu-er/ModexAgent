"""Pool disk specs — renamed framework equivalents of pool_payloads models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.agents.external_coding.paths import ProviderKind
from modex_agent.core.constants import ExecutionStrategyKind
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
    execution_strategy: ExecutionStrategyKind = ExecutionStrategyKind.REACT
    provider_kind: ProviderKind | None = None
    roles: list[str] = Field(default_factory=list)
    """Agent role tags (T1 data layer). Values are plain strings — preset
    values are :class:`modex_agent.core.constants.AgentRole` members, custom
    strings are allowed. Pure metadata透传 to :class:`AgentDescriptor.roles`;
    no runtime behavior change in T1."""
    prompt_name: str | None = None
    """Explicit prompt identifier (decouples prompt identity from agent name).
    ``None`` (default) preserves the agent-name convention — the prompt md
    ``agents/<agent_name>.md`` is used. A non-None value references a different
    prompt md by name. Pure metadata in T1; runtime wiring comes in later
    tickets."""


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
    roles: list[str] = Field(default_factory=list)
    """Agent role tags (T1 data layer). Same contract as
    :attr:`MainAgentSpec.roles`."""
    prompt_name: str | None = None
    """Explicit prompt identifier — same contract as
    :attr:`MainAgentSpec.prompt_name`."""


class PoolSpec(BaseModel):
    """One pool's full disk projection. Rename of PoolTree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    main_agent_name: str
    main: MainAgentSpec
    subagents: list[SubagentSpec] = Field(default_factory=list)
    peers: list[str] = Field(default_factory=list)
    restart_required: bool = False
