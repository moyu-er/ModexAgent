"""Tool approval configuration."""

from pydantic import BaseModel, Field, field_validator

from modex_agent.approval.config import validate_allow_patterns


class ToolApprovalEntry(BaseModel):
    """Per-tool approval rules.

    allowed_paths:
        []      = per-tool approval unless a command exemption matches
        ["*"]   = skip per-tool path prompts
        ["./*"] = exempt paths under the active workspace/project anchor
        These prompt exemptions never expand the sandbox boundary.
    allow_patterns:
        Gray-zone noise-reduction whitelist for command tools (full-command
        regex, case-insensitive ``re.fullmatch`` against the command
        string). A hit classifies NORMAL without a card; deny rules
        always win. Default [] = behavior unchanged.
    """

    allowed_paths: list[str] = []
    allow_patterns: list[str] = []

    _validate = field_validator("allow_patterns")(validate_allow_patterns)


class ApprovalConfig(BaseModel):
    """Agent approval configuration. Default OFF — set ``enabled: true`` to opt in.

    Unlisted tools skip per-tool prompts, not active guard judgments.
    Enabled main-agent approval still escalates sandbox BOUNDARY findings
    with an empty tools map. Disabled approval leaves guard denials active
    without prompts; native subagents have no human approval channel.
    """

    enabled: bool = False
    tools: dict[str, ToolApprovalEntry] = Field(default_factory=dict)
