"""Tests for LLMNode._build_messages() pipeline integration."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from modex_agent.agents.react.nodes.llm import LLMNode
from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.history import ListMessageHistory


class _FakePipeline:
    """Minimal fake pipeline for testing."""
    def __init__(self, content: str) -> None:
        self._content = content

    async def get_or_refresh(self) -> str:
        return self._content


@pytest.mark.asyncio
async def test_build_messages_uses_pipeline_when_available():
    """When system_prompt_pipeline is set, _build_messages uses it."""
    pipeline = _FakePipeline("pipeline prompt")
    ctx = AgentContext(
        system_prompt="static prompt",
        history=ListMessageHistory(),
        tool_manager=MagicMock(),
        session=SessionInfo.from_str("test.agent"),
    )
    ctx.system_prompt_pipeline = pipeline  # type: ignore[assignment]

    node = LLMNode.__new__(LLMNode)
    messages = await node._build_messages(ctx)

    system_msgs = [m for m in messages if m["role"] == "system"]
    assert len(system_msgs) == 1
    assert "pipeline prompt" in system_msgs[0]["content"]


@pytest.mark.asyncio
async def test_build_messages_falls_back_to_static_prompt():
    """When no pipeline, _build_messages uses ctx.system_prompt."""
    ctx = AgentContext(
        system_prompt="static prompt",
        history=ListMessageHistory(),
        tool_manager=MagicMock(),
        session=SessionInfo.from_str("test.agent"),
    )
    ctx.system_prompt_pipeline = None

    node = LLMNode.__new__(LLMNode)
    messages = await node._build_messages(ctx)

    system_msgs = [m for m in messages if m["role"] == "system"]
    assert len(system_msgs) == 1
    assert system_msgs[0]["content"] == "static prompt"


@pytest.mark.asyncio
async def test_build_messages_falls_back_to_static_when_pipeline_empty():
    """When pipeline returns empty string, falls back to ctx.system_prompt.

    The converged ``get_resolved_system_prompt()`` method tries pipeline
    first; if it returns empty, it falls back to the static ``system_prompt``
    field (which holds the 3-provider fallback or ``_DEFAULT_SYSTEM_PROMPT``).
    """
    pipeline = _FakePipeline("")
    ctx = AgentContext(
        system_prompt="static prompt",
        history=ListMessageHistory(),
        tool_manager=MagicMock(),
        session=SessionInfo.from_str("test.agent"),
    )
    ctx.system_prompt_pipeline = pipeline  # type: ignore[assignment]

    node = LLMNode.__new__(LLMNode)
    messages = await node._build_messages(ctx)

    system_msgs = [m for m in messages if m["role"] == "system"]
    assert len(system_msgs) == 1
    assert system_msgs[0]["content"] == "static prompt"
