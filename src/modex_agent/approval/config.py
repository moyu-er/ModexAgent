"""Approval configuration models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ToolApprovalConfig(BaseModel):
    """Per-tool approval configuration.

    allowed_paths: list of path patterns that do NOT require approval.
    Empty list means ALL paths require approval.
    ["*"] means NO paths require approval.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_paths: list[str] = Field(default_factory=list)


class AgentApprovalConfig(BaseModel):
    """Per-agent approval configuration.

    enabled: whether approval checking is active for this agent.
    tools: mapping from tool name to its approval config.
        Tools not in this mapping are NOT subject to approval.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    tools: dict[str, ToolApprovalConfig] = Field(default_factory=dict)
