"""Unit tests for memory/context.py.

TDD: verify ContextState behaviors including system prompt building and history.
"""

from __future__ import annotations

import inspect

import pytest

from modex_agent.memory.context import ContextManager, ContextState
from modex_agent.memory.history import ListMessageHistory


def test_context_load_has_no_legacy_skill_manager_parameter() -> None:
    assert "skill_manager" not in inspect.signature(ContextManager.load).parameters


async def _history_to_list(history):
    if hasattr(history, "to_list"):
        return await history.to_list()
    return list(history)


class _FakePipeline:
    """Minimal fake pipeline for testing."""

    def __init__(self, content: str) -> None:
        self._content = content

    async def get_or_refresh(self) -> str:
        return self._content


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

    @pytest.mark.asyncio
    async def test_to_messages_prefers_pipeline(self):
        """When pipeline is set, to_messages uses it instead of system_prompt."""
        pipeline = _FakePipeline("pipeline content")
        cs = ContextState(
            system_prompt="static prompt",
            system_prompt_pipeline=pipeline,  # type: ignore[arg-type]
        )
        msgs = await cs.to_messages()
        system_msgs = [m for m in msgs if m["role"] == "system"]
        assert len(system_msgs) == 1
        assert "pipeline content" in system_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_to_messages_falls_back_to_system_prompt(self):
        """When pipeline is None, to_messages uses system_prompt."""
        cs = ContextState(system_prompt="static prompt")
        msgs = await cs.to_messages()
        system_msgs = [m for m in msgs if m["role"] == "system"]
        assert len(system_msgs) == 1
        assert system_msgs[0]["content"] == "static prompt"
