"""Unit tests for AgentContext message construction."""

import pytest

from framework.core.agent import AgentContext
from framework.core.tool_manager import InMemoryToolManager
from framework.memory.history import ListMessageHistory


class TestAgentContextToMessages:
    @pytest.mark.asyncio
    async def test_single_system_prompt_no_history(self):
        ctx = AgentContext(
            system_prompt="You are a bot",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
        )
        msgs = await ctx.to_messages()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "You are a bot"

    @pytest.mark.asyncio
    async def test_filters_existing_system_messages_from_history(self):
        ctx = AgentContext(
            system_prompt="Live system prompt",
            history=ListMessageHistory([
                {"role": "system", "content": "[Earlier conversation compressed] summary"},
                {"role": "user", "content": "hi"},
                {"role": "system", "content": "Another stale system msg"},
                {"role": "assistant", "content": "hello"},
            ]),
            tool_manager=InMemoryToolManager(),
        )
        msgs = await ctx.to_messages()
        roles = [m["role"] for m in msgs]
        assert roles.count("system") == 1
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "Live system prompt"
        assert {"role": "user", "content": "hi"} in msgs
        assert {"role": "assistant", "content": "hello"} in msgs
        assert all(
            m["content"] != "[Earlier conversation compressed] summary" for m in msgs
        )

    @pytest.mark.asyncio
    async def test_empty_system_prompt_still_filters_history_system(self):
        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([
                {"role": "system", "content": "stale"},
                {"role": "user", "content": "hi"},
            ]),
            tool_manager=InMemoryToolManager(),
        )
        msgs = await ctx.to_messages()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_tool_messages_preserved(self):
        ctx = AgentContext(
            system_prompt="Sys",
            history=ListMessageHistory([
                {"role": "user", "content": "call tool"},
                {"role": "assistant", "content": "ok", "tool_calls": [{"id": "1"}]},
                {"role": "tool", "content": "result", "tool_call_id": "1"},
            ]),
            tool_manager=InMemoryToolManager(),
        )
        msgs = await ctx.to_messages()
        roles = [m["role"] for m in msgs]
        assert roles == ["system", "user", "assistant", "tool"]
