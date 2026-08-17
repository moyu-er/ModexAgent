from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from bot.adapters import channels
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import ResolvePoolStage, RoutingMeta
from bot.input_pipeline.stages.set_channel import SetChannelStage

from modex_agent.core.session_id import SessionIdFactory, encode_snowflake
from modex_agent.input_pipeline.envelope import UserInputEnvelope
from modex_agent.input_pipeline.stage import Terminate


def _ctx(
    store_get: str = "main", *, store: MagicMock | None = None
) -> BotInputContext:
    pool_store = store if store is not None else MagicMock()
    pool_store.get.return_value = store_get
    return BotInputContext(
        default_pool="main",
        available_pools=lambda: {"main", "coding"},
        pool_session_store=pool_store,
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
    )


@pytest.mark.asyncio
async def test_set_channel_uses_envelope_channel() -> None:
    channels._conversation_channels.clear()
    env = UserInputEnvelope(external_id="u1", content="hi", channel="qq")
    await SetChannelStage().process(env, _ctx())
    # S4 now keys by the encoded snowflake (same as S5 and every downstream
    # lookup) so control command responses route to the correct channel.
    assert channels.get_conv_channel(encode_snowflake("u1")) == "qq"


@pytest.mark.asyncio
async def test_resolve_pool_uses_explicit_pool() -> None:
    env = UserInputEnvelope(
        external_id="u1", content="hi", channel="websocket", explicit_pool="coding"
    )
    await ResolvePoolStage().process(env, _ctx())
    expected_sid = SessionIdFactory().create(agent_name="coding", external_id="u1").session_id
    assert env.metadata["resolved_pool"] == "coding"
    assert env.metadata["resolved_agent"] == "coding"
    assert env.metadata["full_session_id"] == expected_sid


@pytest.mark.asyncio
async def test_resolve_pool_falls_back_to_session_store() -> None:
    env = UserInputEnvelope(
        external_id="u1", content="hi", channel="qq", explicit_pool=None
    )
    await ResolvePoolStage().process(env, _ctx(store_get="coding"))
    expected_sid = SessionIdFactory().create(agent_name="coding", external_id="u1").session_id
    assert env.metadata["resolved_pool"] == "coding"
    assert env.metadata["full_session_id"] == expected_sid


@pytest.mark.asyncio
async def test_resolve_pool_tree_attribution_precedes_session_store_without_persisting() -> None:
    store = MagicMock()
    ctx = _ctx(store_get="main", store=store)
    env = UserInputEnvelope(
        external_id="u1", content="hi", channel="websocket", explicit_pool=None
    )
    env.metadata[RoutingMeta.TREE_RESOLVED_POOL] = "coding"

    await ResolvePoolStage().process(env, ctx)

    assert env.metadata[RoutingMeta.RESOLVED_POOL] == "coding"
    store.get.assert_not_called()
    store.set.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_pool_im_without_tree_attribution_reads_session_store() -> None:
    store = MagicMock()
    ctx = _ctx(store_get="coding", store=store)
    env = UserInputEnvelope(
        external_id="u1", content="hi", channel="qq", explicit_pool=None
    )

    await ResolvePoolStage().process(env, ctx)

    assert env.metadata[RoutingMeta.RESOLVED_POOL] == "coding"
    store.get.assert_called_once_with(encode_snowflake("u1"), "main")
    store.set.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_pool_default_when_store_empty() -> None:
    env = UserInputEnvelope(
        external_id="u1", content="hi", channel="qq", explicit_pool=None
    )
    await ResolvePoolStage().process(env, _ctx(store_get="main"))
    expected_sid = SessionIdFactory().create(agent_name="main", external_id="u1").session_id
    assert env.metadata["full_session_id"] == expected_sid


@pytest.mark.asyncio
async def test_resolve_pool_persists_explicit_pool_choice() -> None:
    store = MagicMock()
    store.get.return_value = "main"
    ctx = BotInputContext(
        default_pool="main",
        available_pools=lambda: {"main", "coding"},
        pool_session_store=store,
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
    )
    env = UserInputEnvelope(
        external_id="u1", content="hi", channel="websocket", explicit_pool="coding"
    )
    await ResolvePoolStage().process(env, ctx)
    store.set.assert_called_once_with(encode_snowflake("u1"), "coding")


@pytest.mark.asyncio
async def test_resolve_pool_does_not_persist_session_store_fallback() -> None:
    store = MagicMock()
    store.get.return_value = "coding"
    ctx = BotInputContext(
        default_pool="main",
        available_pools=lambda: {"main", "coding"},
        pool_session_store=store,
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
    )
    env = UserInputEnvelope(
        external_id="u1", content="hi", channel="qq", explicit_pool=None
    )
    await ResolvePoolStage().process(env, ctx)
    store.set.assert_not_called()
    assert env.metadata["resolved_pool"] == "coding"


@pytest.mark.asyncio
async def test_resolve_pool_terminates_when_no_pool_configured() -> None:
    ctx = BotInputContext(
        default_pool="main",
        available_pools=lambda: set(),
        pool_session_store=MagicMock(),
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
    )
    env = UserInputEnvelope(
        external_id="u1", content="hi", channel="websocket", explicit_pool=None
    )
    result = await ResolvePoolStage().process(env, ctx)
    assert isinstance(result, Terminate)
    assert result.reason == "no_pool_configured"
    assert result.response is not None
    assert "No pool is configured" in result.response["message"]


@pytest.mark.asyncio
async def test_resolve_pool_terminates_when_resolved_pool_unavailable() -> None:
    store = MagicMock()
    store.get.return_value = "ghost"
    ctx = BotInputContext(
        default_pool="main",
        available_pools=lambda: {"main"},
        pool_session_store=store,
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
    )
    env = UserInputEnvelope(
        external_id="u1", content="hi", channel="qq", explicit_pool=None
    )
    result = await ResolvePoolStage().process(env, ctx)
    assert isinstance(result, Terminate)
    assert result.reason == "pool_unavailable"
    assert result.response is not None
    assert "ghost" in result.response["message"]
