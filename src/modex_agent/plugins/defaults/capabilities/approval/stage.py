"""Approval onramp: turn IM /approve · /deny into a structured decision.

Both channels converge on ``metadata[APPROVAL_DECISION]`` -> S8 lifts it onto
``InputMessage.approval_decision`` -> the agent pipeline's single resume branch.

- WebUI builds the decision at its approvals endpoint (content is empty, the
  decision already sits in metadata) -> this stage just marks it resolved.
- IM types ``/approve`` / ``/deny`` -> this stage parses it into the same DTO
  with ``tool_call_id=None`` (decide-next-pending), clears the content, and
  marks the envelope resolved so the terminal stage leaves it alone.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from modex_agent.messaging.models import ApprovalDecisionInput
from modex_agent.pipeline.input.context import InputContext
from modex_agent.pipeline.input.envelope import CommandStatus, UserInputEnvelope
from modex_agent.pipeline.input.stage import Continue, InputStage, StageResult
from modex_agent.pipeline.input.stages.resolve_pool import RoutingMeta
from modex_agent.plugins.defaults.capabilities.approval.response import (
    parse_approval_action,
)


class ApprovalStageConfig(BaseModel):
    """Empty config — the approval stage takes no construction-time config."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ApprovalStage(InputStage):
    async def process(
        self, envelope: UserInputEnvelope, ctx: InputContext
    ) -> StageResult:
        # WebUI: the structured decision is already in metadata (built at the
        # approvals POST). Mark resolved so the terminal stage stays out of it.
        if RoutingMeta.APPROVAL_DECISION in envelope.metadata:
            envelope.command_status = CommandStatus.RESOLVED
            return Continue(value=envelope)

        action = parse_approval_action(envelope.content or "")
        if action is None:
            return Continue(value=envelope)

        envelope.metadata[RoutingMeta.APPROVAL_DECISION] = ApprovalDecisionInput(
            tool_call_id=None, action=action
        )
        envelope.command_status = CommandStatus.RESOLVED
        envelope.content = ""
        return Continue(value=envelope)
