from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import ResolvePoolStage
from bot.input_pipeline.stages.set_channel import SetChannelStage
from bot.adapters import channels
from framework.input_pipeline.envelope import UserInputEnvelope


def _ctx(store_get: str = "main") -> BotInputContext:
    store = MagicMock()
    store.get.return_value = store_get
    return BotInputContext(
        default_pool="main",
        pool_session_store=store,
        agent_pool_map={"main": "main", "coding": "coding"},
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
    )


@pytest.mark.asyncio
async def test_set_channel_uses_envelope_channel() -> None:
    channels._conversation_channels.clear()
    env = UserInputEnvelope(conversation_id="u1", content="hi", channel="qq")
    await SetChannelStage().process(env, _ctx())
    assert channels.get_conv_channel("u1") == "qq"


@pytest.mark.asyncio
async def test_resolve_pool_uses_explicit_pool() -> None:
    env = UserInputEnvelope(
        conversation_id="u1", content="hi", channel="websocket", explicit_pool="coding"
    )
    await ResolvePoolStage().process(env, _ctx())
    assert env.metadata["resolved_pool"] == "coding"
    assert env.metadata["resolved_agent"] == "coding"
    assert env.metadata["full_session_id"] == "u1.coding"


@pytest.mark.asyncio
async def test_resolve_pool_falls_back_to_session_store() -> None:
    env = UserInputEnvelope(
        conversation_id="u1", content="hi", channel="qq", explicit_pool=None
    )
    await ResolvePoolStage().process(env, _ctx(store_get="coding"))
    assert env.metadata["resolved_pool"] == "coding"
    assert env.metadata["full_session_id"] == "u1.coding"


@pytest.mark.asyncio
async def test_resolve_pool_default_when_store_empty() -> None:
    env = UserInputEnvelope(
        conversation_id="u1", content="hi", channel="qq", explicit_pool=None
    )
    await ResolvePoolStage().process(env, _ctx(store_get="main"))
    assert env.metadata["full_session_id"] == "u1.main"


@pytest.mark.asyncio
async def test_resolve_pool_persists_explicit_pool_choice() -> None:
    # WebUI UI selection: explicit_pool must be persisted so PoolRouter routes.
    store = MagicMock()
    store.get.return_value = "main"  # store currently says main
    ctx = BotInputContext(
        default_pool="main",
        pool_session_store=store,
        agent_pool_map={"main": "main", "coding": "coding"},
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
    )
    env = UserInputEnvelope(
        conversation_id="u1", content="hi", channel="websocket", explicit_pool="coding"
    )
    await ResolvePoolStage().process(env, ctx)
    store.set.assert_called_once_with("u1", "coding")


@pytest.mark.asyncio
async def test_resolve_pool_does_not_persist_when_no_explicit_pool() -> None:
    # IM path (explicit_pool=None): must NOT overwrite the stored pool.
    store = MagicMock()
    store.get.return_value = "coding"
    ctx = BotInputContext(
        default_pool="main",
        pool_session_store=store,
        agent_pool_map={"main": "main", "coding": "coding"},
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
    )
    env = UserInputEnvelope(
        conversation_id="u1", content="hi", channel="qq", explicit_pool=None
    )
    await ResolvePoolStage().process(env, ctx)
    store.set.assert_not_called()
    assert env.metadata["resolved_pool"] == "coding"  # read from store
