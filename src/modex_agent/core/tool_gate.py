"""ToolGate — the framework's tool-gating service seam.

A gate classifies each tool call into the shared
:class:`~modex_agent.core.turn.approval_types.ToolClassification`
outcome vocabulary; the pre-execution decision derives via
``ToolClassification.decision``. Approval and guard implementations
live outside core and implement this seam; runtime services and the
ReAct tool node depend only on the ABC.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from modex_agent.core.agent import AgentContext
from modex_agent.core.message import ToolCall
from modex_agent.core.turn.approval_types import ToolClassification
from modex_agent.core.turn.enums import ApprovalDenyPolicy

__all__ = ["ToolGate"]


class ToolGate(ABC):
    """Classify tool calls and define denial behaviour for the turn."""

    @abstractmethod
    def classify(self, tool_call: ToolCall, ctx: AgentContext) -> ToolClassification: ...

    # EXTENSION POINT: override per-agent to CANCEL_TURN if the ReAct loop
    # should terminate after any denied tool (user /deny or unrelated input).
    # Default TOOL_RESULT_ONLY keeps the loop running so the agent can respond.
    default_deny_policy: ApprovalDenyPolicy = ApprovalDenyPolicy.TOOL_RESULT_ONLY
