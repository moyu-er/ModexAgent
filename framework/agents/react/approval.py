"""ApprovalRuntime — typed approval service for ReActAgent."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from framework.approval.config import AgentApprovalConfig
from framework.approval.constants import ApprovalTier
from framework.interceptor.builtin.tool_approval import ArgumentMatcher

if TYPE_CHECKING:
    from framework.agents.react.strategy import SuspendStrategy
    from framework.core.agent import AgentContext
    from framework.core.types import ToolCall


class ApprovalClassifier(Protocol):
    def classify(self, tool_call: ToolCall, ctx: AgentContext[Any]) -> str: ...


@dataclass
class TieredToolApprovalClassifier:
    """Agent-level tool approval classifier driven by path rules.

    Replaces the old name-based matching (hardline/dangerous/sensitive ToolNameMatcher)
    with a configuration-driven approach:
    - approval.enabled=False  → all tools NORMAL
    - tool not in config      → NORMAL
    - path matches allowed    → NORMAL
    - path does not match     → DANGEROUS
    """
    config: AgentApprovalConfig
    argument_matcher: ArgumentMatcher | None = None

    def classify(self, tool_call: ToolCall, ctx: AgentContext[Any]) -> str:
        # 1. Approval disabled for this agent
        if not self.config.enabled:
            return ApprovalTier.NORMAL

        # 2. Tool not configured for approval
        tool_config = self.config.tools.get(tool_call.tool_name)
        if tool_config is None:
            return ApprovalTier.NORMAL

        # 3. Empty allowed_paths means ALL paths require approval
        if not tool_config.allowed_paths:
            return ApprovalTier.DANGEROUS

        # 4. Check path arguments against allowed_paths
        if self.argument_matcher is not None:
            path_allowed = self.argument_matcher.matches(
                tool_call.arguments or {},
                tool_config.allowed_paths,
            )
            if path_allowed:
                return ApprovalTier.NORMAL

        return ApprovalTier.DANGEROUS


@dataclass
class ApprovalRuntime:
    classifier: ApprovalClassifier
    suspend_strategy: SuspendStrategy
    deny_as_cancel: bool = True
