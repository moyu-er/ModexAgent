from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.unsupported_command import UnsupportedCommandStage
from modex_agent.input_pipeline.envelope import CommandStatus, UserInputEnvelope


def _ctx() -> BotInputContext:
    return MagicMock(spec=BotInputContext)


@pytest.mark.asyncio
async def test_unclaimed_slash_command_terminates_with_generic_notice() -> None:
    env = UserInputEnvelope(external_id="u1", content="/foobar arg", channel="qq")
    result = await UnsupportedCommandStage().process(env, _ctx())
    assert not result.should_continue()
    assert "foobar" in result.response["message"]


@pytest.mark.asyncio
async def test_resolved_command_passes_through() -> None:
    env = UserInputEnvelope(external_id="u1", content="/office-expert", channel="qq")
    env.command_status = CommandStatus.RESOLVED
    result = await UnsupportedCommandStage().process(env, _ctx())
    assert result.should_continue()


@pytest.mark.asyncio
async def test_plain_text_passes_through() -> None:
    env = UserInputEnvelope(external_id="u1", content="hello", channel="qq")
    result = await UnsupportedCommandStage().process(env, _ctx())
    assert result.should_continue()


@pytest.mark.asyncio
async def test_empty_content_passes_through() -> None:
    env = UserInputEnvelope(external_id="u1", content="", channel="websocket")
    result = await UnsupportedCommandStage().process(env, _ctx())
    assert result.should_continue()
