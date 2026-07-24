from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.approval import ApprovalStage
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from modex_agent.approval.types import ApprovalAction
from modex_agent.approval.views import ApprovalDecisionInput
from modex_agent.input_pipeline.envelope import CommandStatus, UserInputEnvelope


def _ctx() -> BotInputContext:
    return MagicMock(spec=BotInputContext)


@pytest.mark.asyncio
async def test_approve_text_becomes_structured_decision() -> None:
    env = UserInputEnvelope(external_id="u1", content="/approve", channel="qq")
    result = await ApprovalStage().process(env, _ctx())
    assert result.should_continue()
    assert env.command_status is CommandStatus.RESOLVED
    assert env.content == ""
    assert env.metadata[RoutingMeta.APPROVAL_DECISION] == ApprovalDecisionInput(
        tool_call_id=None, action=ApprovalAction.ALLOW
    )


@pytest.mark.asyncio
async def test_deny_text_becomes_structured_decision() -> None:
    env = UserInputEnvelope(external_id="u1", content="/deny", channel="qq")
    result = await ApprovalStage().process(env, _ctx())
    assert result.should_continue()
    assert env.metadata[RoutingMeta.APPROVAL_DECISION] == ApprovalDecisionInput(
        tool_call_id=None, action=ApprovalAction.DENY
    )


@pytest.mark.asyncio
async def test_existing_decision_passes_and_is_marked_resolved() -> None:
    env = UserInputEnvelope(
        external_id="u1",
        content="",
        channel="websocket",
        metadata={
            RoutingMeta.APPROVAL_DECISION: ApprovalDecisionInput(
                "c1", ApprovalAction.ALLOW
            )
        },
    )
    result = await ApprovalStage().process(env, _ctx())
    assert result.should_continue()
    assert env.command_status is CommandStatus.RESOLVED


@pytest.mark.asyncio
async def test_non_approval_command_passes_through_untouched() -> None:
    env = UserInputEnvelope(external_id="u1", content="/office-expert go", channel="qq")
    result = await ApprovalStage().process(env, _ctx())
    assert result.should_continue()
    assert env.command_status is CommandStatus.UNRESOLVED
    assert RoutingMeta.APPROVAL_DECISION not in env.metadata
    assert env.content == "/office-expert go"