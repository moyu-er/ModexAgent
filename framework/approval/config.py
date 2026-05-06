"""Approval configuration dataclasses."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolApprovalConfig:
    """Per-tool approval configuration.

    allowed_paths: list of path patterns that do NOT require approval.
    Empty list means ALL paths require approval.
    ["*"] means NO paths require approval.
    """
    allowed_paths: list[str] = field(default_factory=list)


@dataclass
class AgentApprovalConfig:
    """Per-agent approval configuration.

    enabled: whether approval checking is active for this agent.
    tools: mapping from tool name to its approval config.
        Tools not in this mapping are NOT subject to approval.
    """
    enabled: bool = False
    tools: dict[str, ToolApprovalConfig] = field(default_factory=dict)
