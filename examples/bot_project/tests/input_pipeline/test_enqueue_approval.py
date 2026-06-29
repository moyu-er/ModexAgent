from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.enqueue import EnqueueStage
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from modex_agent.approval.types import ApprovalAction
from modex_agent.approval.views import ApprovalDecisionInput
from modex_agent.core.types import InputMessage
from modex_agent.input_pipeline.envelope import UserInputEnvelope


def _ctx(enqueued: list[InputMessage]) -> BotInputContext:
    return BotInputContext(
        default_pool="main",
        pool_session_store=MagicMock(),
        agent_pool_map={"main": "main"},
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=enqueued.append,
        command_adapter=MagicMock(),
    )


@pytest.mark.asyncio
async def test_enqueue_lifts_approval_decision_to_input_message() -> None:
    decision = ApprovalDecisionInput("call_1", ApprovalAction.ALLOW)
    enqueued: list[InputMessage] = []
    env = UserInputEnvelope(external_id="u1", content="", channel="websocket")
    env.metadata[RoutingMeta.RESOLVED_AGENT] = "main"
    env.metadata[RoutingMeta.FULL_SESSION_ID] = "u1.main"
    env.metadata[RoutingMeta.APPROVAL_DECISION] = decision
    await EnqueueStage().process(env, _ctx(enqueued))
    assert enqueued[0].approval_decision == decision
    assert enqueued[0].content == ""  # no skill_xml, empty content


@pytest.mark.asyncio
async def test_enqueue_normal_message_has_no_approval_decision() -> None:
    enqueued: list[InputMessage] = []
    env = UserInputEnvelope(external_id="u1", content="hello", channel="websocket")
    env.metadata[RoutingMeta.RESOLVED_AGENT] = "main"
    env.metadata[RoutingMeta.FULL_SESSION_ID] = "u1.main"
    await EnqueueStage().process(env, _ctx(enqueued))
    assert enqueued[0].approval_decision is None
    assert enqueued[0].content == "hello"
