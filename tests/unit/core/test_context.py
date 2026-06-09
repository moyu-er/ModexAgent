"""Unit tests for core/context.py.

TDD: verify ContextState behaviors including system prompt building and history.
"""

import pytest

from framework.core.context import (
    ContextState,
)
from framework.memory.history import ListMessageHistory


async def _history_to_list(history):
    if hasattr(history, "to_list"):
        return await history.to_list()
    return list(history)


class TestContextState:
    @pytest.mark.asyncio
    async def test_to_messages_with_system_prompt(self):
        cs = ContextState(system_prompt="You are a bot", history=ListMessageHistory([{"role": "user", "content": "hi"}]))
        msgs = await cs.to_messages()
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "You are a bot"
        assert msgs[1]["role"] == "user"

    @pytest.mark.asyncio
    async def test_to_messages_without_system_prompt(self):
        cs = ContextState(system_prompt="", history=ListMessageHistory([{"role": "user", "content": "hi"}]))
        msgs = await cs.to_messages()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
