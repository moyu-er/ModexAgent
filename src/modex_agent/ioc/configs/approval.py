"""Tool approval configuration."""

from pydantic import BaseModel, Field


class ToolApprovalEntry(BaseModel):
    """Per-tool approval rules.

    allowed_paths:
        []      = all paths require approval (strictest)
        ["*"]   = all paths auto-allowed (loosest)
        ["./*"] = paths within project dir auto-allowed
    """

    allowed_paths: list[str] = []


class ApprovalConfig(BaseModel):
    """Agent approval configuration. Default OFF — set ``enabled: true`` to opt in.

    Tools NOT listed in `tools` field are auto-allowed without approval.
    """

    enabled: bool = False
    tools: dict[str, ToolApprovalEntry] = Field(default_factory=dict)
