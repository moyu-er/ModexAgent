"""ApprovalRuntime — typed approval service for ReActAgent."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from framework.approval.constants import ApprovalTier
from framework.interceptor.builtin.tool_approval import ArgumentMatcher, ToolNameMatcher

if TYPE_CHECKING:
    from framework.agents.react.strategy import SuspendStrategy
    from framework.core.agent import AgentContext
    from framework.core.types import ToolCall


class ApprovalClassifier(Protocol):
    def classify(self, tool_call: ToolCall, ctx: AgentContext[Any]) -> str: ...


@dataclass
class TieredToolApprovalClassifier:
    hardline: ToolNameMatcher | None = None
    dangerous: ToolNameMatcher | None = None
    sensitive: ToolNameMatcher | None = None
    argument_matcher: ArgumentMatcher | None = None

    def classify(self, tool_call: ToolCall, ctx: AgentContext[Any]) -> str:
        tool_name = tool_call.tool_name

        if self.hardline is not None and self.hardline.matches(tool_name):
            return ApprovalTier.HARDLINE

        is_dangerous = self.dangerous is not None and self.dangerous.matches(tool_name)
        if is_dangerous:
            # Path-based check only applies to tools in the dangerous name set
            if self.argument_matcher is not None and not self.argument_matcher.is_allowed(tool_call):
                return ApprovalTier.DANGEROUS
            return ApprovalTier.DANGEROUS

        if self.sensitive is not None and self.sensitive.matches(tool_name):
            return ApprovalTier.SENSITIVE

        return ApprovalTier.NORMAL


@dataclass
class ApprovalRuntime:
    classifier: ApprovalClassifier
    suspend_strategy: SuspendStrategy
    deny_as_cancel: bool = True
