"""ApprovalRuntime — typed approval service for ReActAgent.

Approval classification (``ApprovalClassifier``) is a policy service;
``ApprovalTransaction`` inside ``ReActTurnState`` owns the state.
``ApprovalDenyPolicy`` defines turn-cancel behaviour for denied approvals.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from modex_agent.approval.config import AgentApprovalConfig
from modex_agent.approval.constants import ApprovalTier
from modex_agent.core.agent import AgentContext
from modex_agent.core.types import ToolCall
from modex_agent.interceptor.builtin.tool_approval import ArgumentMatcher
from modex_agent.runtime.enums import ApprovalDenyPolicy


class ApprovalClassifier(ABC):
    """Classify a tool call into an ``ApprovalTier`` value."""

    @abstractmethod
    def classify(self, tool_call: ToolCall, ctx: AgentContext) -> ApprovalTier: ...


@dataclass
class TieredToolApprovalClassifier(ApprovalClassifier):
    """Agent-level tool approval classifier driven by path rules.

    - approval.enabled=False  → all tools NORMAL
    - tool not in config      → NORMAL
    - path matches allowed    → NORMAL
    - path does not match     → DANGEROUS
    """

    config: AgentApprovalConfig
    argument_matcher: ArgumentMatcher | None = None

    def classify(self, tool_call: ToolCall, ctx: AgentContext) -> ApprovalTier:
        if not self.config.enabled:
            return ApprovalTier.NORMAL

        tool_config = self.config.tools.get(tool_call.tool_name)
        if tool_config is None:
            return ApprovalTier.NORMAL

        if not tool_config.allowed_paths:
            return ApprovalTier.DANGEROUS

        if "*" in tool_config.allowed_paths:
            return ApprovalTier.NORMAL

        if self.argument_matcher is not None:
            args = tool_call.arguments or {}
            if not self.argument_matcher._extract_paths(args):
                return ApprovalTier.DANGEROUS
            path_allowed = self.argument_matcher.matches(args, tool_config.allowed_paths)
            if path_allowed:
                return ApprovalTier.NORMAL

        return ApprovalTier.DANGEROUS


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
