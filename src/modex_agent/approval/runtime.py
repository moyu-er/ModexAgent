"""ApprovalRuntime — typed approval service for tool execution.

Approval classification (``ApprovalClassifier``) is a policy service;
``ApprovalTransaction`` inside ``ReActTurnState`` owns the state.
``ApprovalDenyPolicy`` defines turn-cancel behaviour for denied approvals.

These contracts use approval, core, interceptor, and runtime types without
importing the ReAct implementation. Guard classification shares this surface;
independent enablement does not imply dependency-free packages.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from modex_agent.approval.classification import ToolClassification
from modex_agent.approval.config import AgentApprovalConfig
from modex_agent.approval.constants import ApprovalTier
from modex_agent.core.agent import AgentContext
from modex_agent.core.message import ToolCall
from modex_agent.interceptor.builtin.tool_approval import ArgumentMatcher
from modex_agent.runtime.enums import ApprovalDenyPolicy


class ApprovalClassifier(ABC):
    """Classify a tool call into one typed :class:`ToolClassification`.

    The returned value is pure data — tier, typed source (tier rules vs
    guard verdict), guard category, deny-side reason, and the audit fact
    when the guard decided. Classifiers hold no mutable reason state and
    perform no writes or suspension; ToolNode derives decisions, denial
    results, pending approvals, and audit rows from the stored value.
    """

    @abstractmethod
    def classify(self, tool_call: ToolCall, ctx: AgentContext) -> ToolClassification: ...


def command_matches_allow_patterns(command: str, allow_patterns: list[str]) -> bool:
    """Check full-command regex exemptions from per-tool approval prompts.

    Full-command regex matching: each pattern is a case-insensitive
    ``re.fullmatch`` against the whole command string, so an allowed
    prefix cannot approve an appended shell operation. Only de-noises
    — a hit downgrades the tier to NORMAL; it never overrides a deny
    verdict (deny rules are evaluated earlier, in the guard layer
    wrapping this classifier). Patterns are validated at the config
    boundary, so they always compile.
    """
    if not allow_patterns:
        return False
    lowered = command.lower()
    return any(re.fullmatch(p.lower(), lowered) for p in allow_patterns)


@dataclass
class TieredToolApprovalClassifier(ApprovalClassifier):
    """Agent-level tool approval classifier driven by path rules.

    - approval.enabled=False  -> all tools NORMAL
    - tool not in config      -> NORMAL
    - command hits allow_patterns -> NORMAL (gray-zone de-noising; deny
      rules live in the guard layer and always win)
    - path matches allowed    -> NORMAL
    - path does not match     -> DANGEROUS

    These are inner-tier rules only. An outer SecurityClassifier evaluates
    guards first; NORMAL here does not authorize crossing its boundary.
    """

    config: AgentApprovalConfig
    argument_matcher: ArgumentMatcher | None = None

    def _tier(self, tool_call: ToolCall, ctx: AgentContext) -> ApprovalTier:
        if not self.config.enabled:
            return ApprovalTier.NORMAL

        tool_config = self.config.tools.get(tool_call.tool_name)
        if tool_config is None:
            return ApprovalTier.NORMAL

        args = tool_call.arguments or {}
        command = args.get("command")
        if isinstance(command, str) and command_matches_allow_patterns(
            command, tool_config.allow_patterns
        ):
            return ApprovalTier.NORMAL

        if not tool_config.allowed_paths:
            return ApprovalTier.DANGEROUS

        if "*" in tool_config.allowed_paths:
            return ApprovalTier.NORMAL

        if self.argument_matcher is not None:
            if not self.argument_matcher._extract_paths(args):
                return ApprovalTier.DANGEROUS
            path_allowed = self.argument_matcher.matches(args, tool_config.allowed_paths)
            if path_allowed:
                return ApprovalTier.NORMAL

        return ApprovalTier.DANGEROUS

    def classify(self, tool_call: ToolCall, ctx: AgentContext) -> ToolClassification:
        return ToolClassification.tier_result(self._tier(tool_call, ctx))


@dataclass
class ApprovalRuntime:
    """Approval policy service — classification + deny behaviour.

    ``ApprovalTransaction`` inside ``ReActTurnState`` owns state and persistence;
    this service only classifies tools and defines denial behaviour.
    """

    classifier: ApprovalClassifier
    # EXTENSION POINT: override per-agent to CANCEL_TURN if the ReAct loop
    # should terminate after any denied tool (user /deny or unrelated input).
    # Default TOOL_RESULT_ONLY keeps the loop running so the agent can respond.
    default_deny_policy: ApprovalDenyPolicy = ApprovalDenyPolicy.TOOL_RESULT_ONLY
