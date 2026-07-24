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

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from modex_agent.approval.response import parse_approval_action
from modex_agent.approval.views import ApprovalDecisionInput
from modex_agent.input_pipeline.envelope import CommandStatus, UserInputEnvelope
from modex_agent.input_pipeline.stage import Continue, InputStage, StageResult


class ApprovalStage(InputStage):
    async def process(
        self, envelope: UserInputEnvelope, ctx: BotInputContext
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
