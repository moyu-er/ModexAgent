"""Unit tests for core/context.py.

TDD: verify InMemoryContextManager, FileContextManager, and ContextState
behaviors including persistence, system prompt building, and history trimming.
"""

import tempfile
from pathlib import Path

import pytest

from framework.core.context import (
    ContextState,
    EphemeralContextManager,
    FileContextManager,
    InMemoryContextManager,
)
from framework.core.emitter import AgentResult
from framework.core.tool_manager import FunctionalTool, InMemoryToolManager
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


class TestInMemoryContextManager:
    @pytest.fixture
    def cm(self):
        return InMemoryContextManager(base_system_prompt="Base prompt")

    @pytest.mark.asyncio
    async def test_load_creates_new_session(self, cm):
        state = await cm.load("s1")
        assert state.system_prompt == "Base prompt"
        assert await _history_to_list(state.history) == []

    @pytest.mark.asyncio
    async def test_save_appends_user_agent_appends_assistant(self, cm):
        await cm.load("s1")
        await cm.save("s1", {"role": "user", "content": "hi"}, AgentResult())
        state = await cm.load("s1")
        await state.history.append({"role": "assistant", "content": "Hello"})
        history = await _history_to_list(state.history)
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "hi"
        assert history[1]["role"] == "assistant"
        assert history[1]["content"] == "Hello"

    @pytest.mark.asyncio
    async def test_save_appends_user_agent_appends_assistant_and_tool(self, cm):
        await cm.load("s1")
        await cm.save("s1", {"role": "user", "content": "hi"}, AgentResult())
        state = await cm.load("s1")
        await state.history.append({"role": "assistant", "content": "ok"})
        await state.history.append({"role": "tool", "content": "result"})
        history = await _history_to_list(state.history)
        assert len(history) == 3  # user + assistant + tool
        assert history[1]["role"] == "assistant"
        assert history[2]["role"] == "tool"

    @pytest.mark.asyncio
    async def test_save_appends_user_and_filters_system_from_history(self, cm):
        await cm.load("s1")
        await cm.save("s1", {"role": "user", "content": "hi"}, AgentResult())
        state = await cm.load("s1")
        await state.history.append({"role": "system", "content": "compressed summary"})
        await state.history.append({"role": "assistant", "content": "ok"})
        history = await _history_to_list(state.history)
        roles = [m["role"] for m in history]
        # AgentContext.to_messages filters system from history, but history itself keeps them
        assert roles == ["user", "system", "assistant"]
        assert all(m.get("content") != "compressed summary" for m in history) is False

    @pytest.mark.asyncio
    async def test_build_system_prompt_with_tools_not_in_prompt(self, cm):
        """Tool descriptions are passed via API tools param, not system prompt."""
        tm = InMemoryToolManager()
        tm.register(
            FunctionalTool(
                name="weather",
                description="Get weather",
                parameters={"type": "object", "properties": {}},
                func=lambda: "sunny",
            )
        )
        prompt = await cm.build_system_prompt(tool_manager=tm)
        assert "Base prompt" in prompt
        assert "weather" not in prompt  # tools not injected into system prompt

    @pytest.mark.asyncio
    async def test_build_system_prompt_with_runtime_info(self, cm):
        prompt = await cm.build_system_prompt(
            tool_manager=None,
            runtime_info={"current_time": "12:00", "platform": "win32"},
        )
        assert "Base prompt" in prompt
        assert "12:00" in prompt
        assert "win32" in prompt

    @pytest.mark.asyncio
    async def test_clear_removes_session(self, cm):
        await cm.load("s1")
        await cm.clear("s1")
        state = await cm.load("s1")
        assert await _history_to_list(state.history) == []


class TestFileContextManager:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    @pytest.fixture
    def cm(self, tmp_dir):
        return FileContextManager(base_system_prompt="File prompt", data_dir=tmp_dir, max_history=2)

    @pytest.mark.asyncio
    async def test_load_from_file_after_save(self, cm, tmp_dir):
        await cm.load("s1")
        await cm.save("s1", {"role": "user", "content": "hi"}, AgentResult())
        state = await cm.load("s1")
        await state.history.append({"role": "assistant", "content": "Reply"})
        # Trigger file persistence after agent appends its own message
        await cm.save("s1", None, AgentResult())

        # Create a new manager instance pointing to the same directory
        cm2 = FileContextManager(base_system_prompt="File prompt", data_dir=tmp_dir, max_history=2)
        state = await cm2.load("s1")
        history = await _history_to_list(state.history)
        assert len(history) == 2
        assert history[0]["content"] == "hi"
        assert history[1]["content"] == "Reply"

    @pytest.mark.asyncio
    async def test_file_naming_uses_hash_for_long_session_id(self, cm, tmp_dir):
        long_id = "a" * 150
        await cm.load(long_id)
        result = AgentResult(content="x", stop_reason="complete")
        await cm.save(long_id, {"role": "user", "content": "h"}, result)

        files = list(tmp_dir.iterdir())
        assert len(files) == 1
        # Should be an md5 hash (32 hex chars) + .json
        assert len(files[0].stem) == 32

    @pytest.mark.asyncio
    async def test_history_trimming(self, cm):
        await cm.load("s1")
        for i in range(5):
            await cm.save("s1", {"role": "user", "content": f"u{i}"}, AgentResult())
            state = await cm.load("s1")
            await state.history.append({"role": "assistant", "content": f"a{i}"})
            # Trigger persistence + trimming after agent appends its message
            await cm.save("s1", None, AgentResult())
        state = await cm.load("s1")
        history = await _history_to_list(state.history)
        # max_history=2 means 4 messages kept (2 user + 2 assistant)
        assert len(history) == 4
        assert history[-2]["content"] == "u4"
        assert history[-1]["content"] == "a4"

    @pytest.mark.asyncio
    async def test_clear_deletes_file(self, cm, tmp_dir):
        await cm.load("s1")
        await cm.save("s1", {"role": "user", "content": "h"}, AgentResult(content="x", stop_reason="complete"))
        files_before = list(tmp_dir.iterdir())
        assert len(files_before) == 1

        await cm.clear("s1")
        files_after = list(tmp_dir.iterdir())
        assert len(files_after) == 0

    @pytest.mark.asyncio
    async def test_save_with_tool_messages(self, cm, tmp_dir):
        await cm.load("s1")
        await cm.save("s1", {"role": "user", "content": "hi"}, AgentResult())
        state = await cm.load("s1")
        await state.history.append({"role": "assistant", "content": "call tool"})
        await state.history.append({"role": "tool", "content": "done"})
        # Trigger file persistence after agent appends its own messages
        await cm.save("s1", None, AgentResult())

        # Re-read from disk
        cm2 = FileContextManager(base_system_prompt="File prompt", data_dir=tmp_dir, max_history=10)
        state = await cm2.load("s1")
        history = await _history_to_list(state.history)
        assert len(history) == 3
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"
        assert history[2]["role"] == "tool"

    @pytest.mark.asyncio
    async def test_save_and_load_filters_system_messages(self, cm, tmp_dir):
        await cm.load("s1")
        await cm.save("s1", {"role": "user", "content": "hi"}, AgentResult())
        state = await cm.load("s1")
        await state.history.append({"role": "system", "content": "stale system"})
        await state.history.append({"role": "assistant", "content": "ok"})
        await state.history.append({"role": "system", "content": "another stale"})
        # Trigger file persistence after agent appends its own messages
        await cm.save("s1", None, AgentResult())

        cm2 = FileContextManager(base_system_prompt="File prompt", data_dir=tmp_dir, max_history=10)
        state = await cm2.load("s1")
        history = await _history_to_list(state.history)
        roles = [m["role"] for m in history]
        assert roles == ["user", "system", "assistant", "system"]
        assert all(m.get("content") != "stale system" for m in history) is False


class TestEphemeralContextManager:
    @pytest.fixture
    def cm(self):
        return EphemeralContextManager(base_system_prompt="Ephemeral prompt")

    @pytest.mark.asyncio
    async def test_save_and_load_work_during_turn(self, cm):
        await cm.load("s1")
        await cm.save("s1", {"role": "user", "content": "hi"}, AgentResult())
        state = await cm.load("s1")
        await state.history.append({"role": "assistant", "content": "Hello"})
        history = await _history_to_list(state.history)
        assert len(history) == 2
        assert history[0]["content"] == "hi"
        assert history[1]["content"] == "Hello"

    @pytest.mark.asyncio
    async def test_clear_removes_all_data(self, cm):
        await cm.load("s1")
        await cm.save("s1", {"role": "user", "content": "hi"}, AgentResult(content="ok"))
        await cm.clear("s1")
        state = await cm.load("s1")
        assert await _history_to_list(state.history) == []
