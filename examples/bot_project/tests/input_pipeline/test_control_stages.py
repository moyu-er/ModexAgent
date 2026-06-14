from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.environment_control import EnvironmentControlStage
from bot.input_pipeline.stages.session_control import SessionControlStage
from framework.input_pipeline.envelope import UserInputEnvelope


def _ctx(
    *,
    store_get: str = "main",
    command_handled: bool = False,
) -> BotInputContext:
    store = MagicMock()
    store.get.return_value = store_get
    cmd_adapter = MagicMock()
    cmd_adapter._try_intercept_control = AsyncMock(return_value=command_handled)
    return BotInputContext(
        default_pool="main",
        pool_session_store=store,
        agent_pool_map={"main": "main", "coding": "coding"},
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=cmd_adapter,
    )


@pytest.mark.asyncio
async def test_pool_command_terminates_and_records_pool() -> None:
    """S2: /pool shortcut (e.g. /coding) records pool and terminates."""
    ctx = _ctx()
    env = UserInputEnvelope(conversation_id="u1", content="/coding", channel="qq")
    stage = EnvironmentControlStage(known_pools={"coding"})
    result = await stage.process(env, ctx)

    assert result.should_continue() is False
    ctx.pool_session_store.set.assert_called_once_with("u1", "coding")


@pytest.mark.asyncio
async def test_environment_command_delegates_to_adapter_and_terminates() -> None:
    """S2: /cd etc delegates to adapter, terminates when handled."""
    ctx = _ctx(command_handled=True)
    env = UserInputEnvelope(conversation_id="u1", content="/cd /tmp", channel="qq")
    stage = EnvironmentControlStage()
    result = await stage.process(env, ctx)

    assert result.should_continue() is False
    ctx.command_adapter._try_intercept_control.assert_awaited_once_with(
        "/cd /tmp", "u1"
    )


@pytest.mark.asyncio
async def test_normal_message_passes_through() -> None:
    """S2: ordinary text passes through to next stage."""
    ctx = _ctx()
    env = UserInputEnvelope(conversation_id="u1", content="hello", channel="qq")
    stage = EnvironmentControlStage()
    result = await stage.process(env, ctx)

    assert result.should_continue() is True


@pytest.mark.asyncio
async def test_environment_stage_does_not_handle_stop() -> None:
    """S2: /stop passes through — S3 owns it."""
    ctx = _ctx(command_handled=True)
    env = UserInputEnvelope(conversation_id="u1", content="/stop", channel="qq")
    stage = EnvironmentControlStage()
    result = await stage.process(env, ctx)

    assert result.should_continue() is True
    ctx.command_adapter._try_intercept_control.assert_not_called()


@pytest.mark.asyncio
async def test_stop_command_handled_by_session_stage() -> None:
    """S3: /stop resolves full_session_id and delegates to adapter."""
    ctx = _ctx(store_get="coding", command_handled=True)
    env = UserInputEnvelope(conversation_id="u1", content="/stop", channel="qq")
    stage = SessionControlStage()
    result = await stage.process(env, ctx)

    assert result.should_continue() is False
    ctx.command_adapter._try_intercept_control.assert_awaited_once_with(
        "/stop", "u1.coding"
    )


@pytest.mark.asyncio
async def test_session_stage_passes_non_stop() -> None:
    """S3: non-/stop messages pass through."""
    ctx = _ctx(command_handled=True)
    env = UserInputEnvelope(conversation_id="u1", content="hello", channel="qq")
    stage = SessionControlStage()
    result = await stage.process(env, ctx)

    assert result.should_continue() is True
    ctx.command_adapter._try_intercept_control.assert_not_called()


@pytest.mark.asyncio
async def test_environment_stage_delegates_pwd_to_adapter() -> None:
    """S2 delegates /pwd to _try_intercept_control and terminates when handled."""
    ctx = _ctx(command_handled=True)
    env = UserInputEnvelope(conversation_id="u1", content="/pwd", channel="qq")
    result = await EnvironmentControlStage(known_pools={"main", "coding"}).process(env, ctx)
    assert not result.should_continue(), "/pwd must terminate when handled"
    ctx.command_adapter._try_intercept_control.assert_awaited_once_with("/pwd", "u1")
