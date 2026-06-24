"""Verify pipeline construction in MemorySystemContextManager.load()."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.context import ContextState
from modex_agent.memory.core.message import ChatMessage
from modex_agent.memory.system import MemorySystemContextManager


def _make_mock_system() -> MagicMock:
    """Create a mock MemorySystem with all async methods load() needs.

    load() now builds a pipeline-specific FullInjectionPolicy that calls
    assemble() internally, so the mock must support:
    ensure_within_budget, retrieve_knowledge, get_knowledge_directory,
    get_storage_path, get_providers, prefetch_memories, get_history,
    create_message_history.
    """
    mock_system = MagicMock()
    mock_system.ensure_within_budget = AsyncMock()
    mock_system.retrieve_knowledge = AsyncMock(
        return_value=MagicMock(soul="", user="", memory=""),
    )
    mock_system.get_knowledge_directory = AsyncMock(return_value=None)
    mock_system.get_storage_path = AsyncMock(return_value=None)
    mock_system.get_providers = MagicMock(return_value=[])
    mock_system.prefetch_memories = AsyncMock(return_value=None)
    mock_system.get_history = AsyncMock(return_value=[])
    mock_system.create_message_history = MagicMock(return_value=MagicMock())
    # Explicitly set to None so duck-typed attribute checks work correctly
    # (MagicMock auto-creates attributes, which would fool hasattr checks).
    mock_system.pruned_manager = None
    return mock_system


@pytest.mark.asyncio
async def test_load_produces_complete_context_state():
    """load() returns ContextState with system_prompt, history, and pipeline."""
    mock_system = _make_mock_system()
    ctx_mgr = MemorySystemContextManager(
        memory_system=mock_system,
        base_system_prompt="You are helpful.",
    )
    state = await ctx_mgr.load("s1", tool_manager=MagicMock())
    assert state.history is not None
    # Pipeline is the sole system prompt path; static fallback is empty
    assert state.system_prompt_pipeline is not None
    prompt = await state.system_prompt_pipeline.get_or_refresh()
    assert "You are helpful." in prompt


@pytest.mark.asyncio
async def test_build_system_prompt_delegates_to_load():
    """build_system_prompt() delegates to load() and returns system_prompt."""
    mock_system = _make_mock_system()
    ctx_mgr = MemorySystemContextManager(
        memory_system=mock_system,
        base_system_prompt="base",
    )
    prompt = await ctx_mgr.build_system_prompt(tool_manager=MagicMock())
    assert "base" in prompt
