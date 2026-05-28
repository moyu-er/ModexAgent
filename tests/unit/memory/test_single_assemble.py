"""Verify single assemble in MemorySystemContextManager."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.core.context import ContextState
from framework.memory.core.message import ChatMessage
from framework.memory.core.models import InjectionResult
from framework.memory.system import MemorySystemContextManager


@pytest.mark.asyncio
async def test_load_produces_complete_context_state():
    """load() returns ContextState with both system_prompt and history."""
    mock_system = MagicMock()
    mock_system.ensure_within_budget = AsyncMock()
    mock_system.create_message_history = MagicMock(
        return_value=MagicMock()
    )
    policy = MagicMock()
    policy.assemble = AsyncMock(return_value=InjectionResult(
        system_prompt="## Knowledge\n...",
        messages=[ChatMessage(role="user", content="hello")],
    ))
    ctx_mgr = MemorySystemContextManager(
        memory_system=mock_system,
        injection_policy=policy,
        base_system_prompt="You are helpful.",
    )
    state = await ctx_mgr.load("s1", tool_manager=MagicMock())
    assert isinstance(state, ContextState)
    assert "You are helpful." in state.system_prompt
    assert "## Knowledge" in state.system_prompt
    assert state.history is not None


@pytest.mark.asyncio
async def test_build_system_prompt_delegates_to_load():
    """build_system_prompt() delegates to load() and returns system_prompt."""
    mock_system = MagicMock()
    mock_system.ensure_within_budget = AsyncMock()
    mock_system.create_message_history = MagicMock(return_value=MagicMock())
    policy = MagicMock()
    policy.assemble = AsyncMock(return_value=InjectionResult(
        system_prompt="test",
        messages=[],
    ))
    ctx_mgr = MemorySystemContextManager(
        memory_system=mock_system,
        injection_policy=policy,
        base_system_prompt="base",
    )
    prompt = await ctx_mgr.build_system_prompt(tool_manager=MagicMock())
    assert "base" in prompt
    assert "test" in prompt
