"""Pool disk specs — renamed framework equivalents of pool_payloads models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modex_agent.agents.external.paths import ProviderKind
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


def _validate_execution_provider_pair(
    execution_strategy: ExecutionStrategyKind,
    provider_kind: ProviderKind | None,
) -> None:
    """Enforce ``provider_kind`` set iff ``execution_strategy == EXTERNAL``.

    Shared cross-field rule for :class:`MainAgentSpec` and :class:`SubagentSpec`.
    A non-EXTERNAL strategy must not carry a ``provider_kind`` (the
    field is meaningless without an external backend), and an EXTERNAL
    strategy must declare one (the harness needs to know which CLI to spawn).
    Raising ``ValueError`` lets pydantic surface it as a ``ValidationError``.
    """
    if execution_strategy == ExecutionStrategyKind.EXTERNAL:
        if provider_kind is None:
            raise ValueError("provider_kind must be set when execution_strategy='external'")
    elif provider_kind is not None:
        raise ValueError(
            "provider_kind must be None when execution_strategy="
            f"{execution_strategy!r} (only 'external' uses a provider)"
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

    @model_validator(mode="after")
    def _validate(self) -> MainAgentSpec:
        _validate_execution_provider_pair(self.execution_strategy, self.provider_kind)
        return self


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
    execution_strategy: ExecutionStrategyKind = ExecutionStrategyKind.REACT
    provider_kind: ProviderKind | None = None
    roles: list[str] = Field(default_factory=list)
    """Agent role tags (T1 data layer). Same contract as
    :attr:`MainAgentSpec.roles`."""
    prompt_name: str | None = None
    """Explicit prompt identifier — same contract as
    :attr:`MainAgentSpec.prompt_name`."""

    @model_validator(mode="after")
    def _validate(self) -> SubagentSpec:
        _validate_execution_provider_pair(self.execution_strategy, self.provider_kind)
        return self


class PoolSpec(BaseModel):
    """One pool's full disk projection. Rename of PoolTree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    main_agent_name: str
    main: MainAgentSpec
    subagents: list[SubagentSpec] = Field(default_factory=list)
    peers: list[str] = Field(default_factory=list)
    restart_required: bool = False
