from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.enqueue import EnqueueStage
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from framework.core.types import InputMessage
from framework.input_pipeline.envelope import UserInputEnvelope


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
async def test_enqueue_uses_raw_content_when_no_skill_xml() -> None:
    enqueued: list[InputMessage] = []
    env = UserInputEnvelope(external_id="u1", content="hi", channel="qq")
    env.metadata[RoutingMeta.RESOLVED_AGENT] = "main"
    env.metadata[RoutingMeta.FULL_SESSION_ID] = "u1.main"
    await EnqueueStage().process(env, _ctx(enqueued))
    assert len(enqueued) == 1
    assert enqueued[0].content == "hi"
    assert enqueued[0].session.agent_name == "main"


@pytest.mark.asyncio
async def test_enqueue_uses_skill_xml_when_present() -> None:
    enqueued: list[InputMessage] = []
    env = UserInputEnvelope(
        external_id="u1", content="/office-expert make ppt", channel="qq"
    )
    env.metadata[RoutingMeta.RESOLVED_AGENT] = "main"
    env.metadata[RoutingMeta.FULL_SESSION_ID] = "u1.main"
    env.metadata["skill_xml"] = "<skill>...</skill>"
    await EnqueueStage().process(env, _ctx(enqueued))
    assert enqueued[0].content == "<skill>...</skill>"


@pytest.mark.asyncio
async def test_enqueue_carries_attachments() -> None:
    from framework.input_pipeline.envelope import AttachmentRef

    enqueued: list[InputMessage] = []
    env = UserInputEnvelope(external_id="u1", content="hi", channel="qq")
    env.metadata[RoutingMeta.RESOLVED_AGENT] = "main"
    env.metadata[RoutingMeta.FULL_SESSION_ID] = "u1.main"
    env.attachments = [AttachmentRef(local_path="/tmp/a.png")]
    await EnqueueStage().process(env, _ctx(enqueued))
    assert enqueued[0].attachments == ["/tmp/a.png"]


@pytest.mark.asyncio
async def test_enqueue_passes_source_and_chat_id() -> None:
    # PoolRouter._route_to_pool reads msg.source (AgentAddress name) and
    # msg.chat_id (broker header). EnqueueStage MUST carry them through.
    enqueued: list[InputMessage] = []
    env = UserInputEnvelope(external_id="u1", content="hi", channel="qq")
    env.metadata[RoutingMeta.RESOLVED_AGENT] = "main"
    env.metadata[RoutingMeta.FULL_SESSION_ID] = "u1.main"
    env.metadata["chat_id"] = "group123"
    await EnqueueStage().process(env, _ctx(enqueued))
    assert enqueued[0].source == "qq"          # == envelope.channel (semantically same)
    assert enqueued[0].chat_id == "group123"   # from metadata, not dropped to default
