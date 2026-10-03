"""Approval configuration models."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


def validate_allow_patterns(patterns: list[str]) -> list[str]:
    """Shared boundary check: every allow pattern must be a compilable regex.

    Both approval config models (runtime ``ToolApprovalConfig`` and the
    YAML ``ToolApprovalEntry`` face) validate through this one function,
    so a broken pattern fails fast at parse time with a useful message
    instead of surfacing as a runtime error on the approval path.
    Matching semantics: case-insensitive ``re.fullmatch`` against the
    whole command string.
    """
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            message = f"invalid allow_patterns regex {pattern!r}: {exc}"
            raise ValueError(message) from exc
    return patterns


class ToolApprovalConfig(BaseModel):
    """Per-tool approval configuration.

    allowed_paths: list of path patterns that do NOT require approval.
    Empty list requires per-tool approval unless a command exemption matches.
    ["*"] skips per-tool path prompts; neither form expands guard boundaries.

    allow_patterns: full-command regex patterns matched case-insensitively
    (``re.fullmatch``) against the command string of command-like tools —
    a hit classifies NORMAL without a card. Same shape as
    ``CommandPatternGuard``'s allow rules, but strictly de-noising: it
    NEVER overrides a deny verdict — the guard's deny rules are
    evaluated before this whitelist and always win. Empty list (default)
    provides no command exemptions.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_paths: list[str] = Field(default_factory=list)
    allow_patterns: list[str] = Field(default_factory=list)

    _validate = field_validator("allow_patterns")(validate_allow_patterns)


class AgentApprovalConfig(BaseModel):
    """Per-agent approval configuration.

    enabled: whether human approval is enabled for this agent.
    tools: mapping from tool name to its approval config.
        Unlisted tools skip per-tool prompts, not active guard judgments.
    An explicit sandbox can escalate main-agent BOUNDARY findings even with
    an empty mapping. Disabled approval keeps guard denials without prompts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    tools: dict[str, ToolApprovalConfig] = Field(default_factory=dict)


class ToolApprovalEntry(BaseModel):
    """Per-tool approval rules (YAML face).

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

    # Same strictness as the runtime face (rule 12): the raw declaration
    # is validated through this model at compile time, so unknown keys
    # fail loudly there instead of being silently dropped.
    model_config = ConfigDict(frozen=True, extra="forbid")

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

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    tools: dict[str, ToolApprovalEntry] = Field(default_factory=dict)
