"""ApprovalRuntime — typed approval service for ReActAgent.

Approval classification (``ApprovalClassifier``) is a policy service;
``ApprovalTransaction`` inside ``ReActTurnState`` owns the state.
``ApprovalDenyPolicy`` replaces the old ``deny_as_cancel: bool`` flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from framework.approval.config import AgentApprovalConfig
from framework.approval.constants import ApprovalTier
from framework.core.agent import AgentContext
from framework.core.types import ToolCall
from framework.interceptor.builtin.tool_approval import ArgumentMatcher
from framework.runtime.enums import ApprovalDenyPolicy

if TYPE_CHECKING:
    from framework.agents.react.strategy import SuspendStrategy


class ApprovalClassifier(Protocol):
    """Classify a tool call into an ``ApprovalTier`` value."""

    def classify(self, tool_call: ToolCall, ctx: AgentContext) -> str: ...


@dataclass
class TieredToolApprovalClassifier:
    """Agent-level tool approval classifier driven by path rules.

    - approval.enabled=False  → all tools NORMAL
    - tool not in config      → NORMAL
    - path matches allowed    → NORMAL
    - path does not match     → DANGEROUS
    """

    config: AgentApprovalConfig
    argument_matcher: ArgumentMatcher | None = None

    def classify(self, tool_call: ToolCall, ctx: AgentContext) -> str:
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

    ``ApprovalTransaction`` inside ``ReActTurnState`` owns the state and
    persistence; this service only classifies tools.

    .. deprecated::
        ``suspend_strategy`` is retained for backward compat during migration.
        New code should use ``TurnStateSuspendStrategy`` from
        ``framework.agents.react.strategy``, wired through ``AgentRuntimeServices``.
    """

    classifier: ApprovalClassifier
    default_deny_policy: ApprovalDenyPolicy = ApprovalDenyPolicy.CANCEL_TURN
    suspend_strategy: SuspendStrategy | None = None
