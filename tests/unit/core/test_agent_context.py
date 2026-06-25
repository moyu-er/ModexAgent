"""Unit tests for AgentContext message construction."""

import pytest

from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.memory.history import ListMessageHistory


class TestAgentContextToMessages:
    @pytest.mark.asyncio
    async def test_single_system_prompt_no_history(self):
        ctx = AgentContext(
            system_prompt="You are a bot",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str("test.agent"),
        )
        msgs = await ctx.to_messages()
        assert len(msgs) == 0  # system prompt no longer included

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
            session=SessionInfo.from_str("test.agent"),
        )
        msgs = await ctx.to_messages()
        roles = [m["role"] for m in msgs]
        assert "system" not in roles  # system messages not in to_messages() output
        assert any(m.get("role") == "user" and m.get("content") == "hi" for m in msgs)
        assert any(m.get("role") == "assistant" and m.get("content") == "hello" for m in msgs)
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
            session=SessionInfo.from_str("test.agent"),
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
            session=SessionInfo.from_str("test.agent"),
        )
        msgs = await ctx.to_messages()
        roles = [m["role"] for m in msgs]
        assert roles == ["user", "assistant", "tool"]  # system prompt no longer included
