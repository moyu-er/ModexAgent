from __future__ import annotations

from unittest.mock import MagicMock

from bot.input_pipeline.context import BotInputContext


def test_bot_input_context_default_pool() -> None:
    ctx = BotInputContext(
        default_pool="main",
        pool_session_store=MagicMock(),
        agent_pool_map={"main": "main"},
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
    )
    assert ctx.default_pool == "main"
    assert ctx.agent_for_pool("main") == "main"


def test_agent_for_pool_falls_back_to_default() -> None:
    ctx = BotInputContext(
        default_pool="main",
        pool_session_store=MagicMock(),
        agent_pool_map={"coding": "coding"},
        agent_resolver=lambda p: "main",
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
    )
    # unknown pool -> default
    assert ctx.agent_for_pool("unknown") == "main"
