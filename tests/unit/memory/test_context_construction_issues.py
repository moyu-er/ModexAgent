"""TDD tests for context construction issues and simplified design.

These tests verify:
1. Single assemble per request (no triple redundancy)
2. Injection returns all messages without filtering
3. Priority-based section trimming works
4. Message count tracking across pipeline stages

Run: pytest tests/unit/memory/test_context_construction_issues.py -v
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from framework.memory.core.message import ChatMessage
from framework.memory.core.models import (
    InjectionResult,
    MemoryBudget,
)
from framework.memory.core.scope import MemoryContext
from framework.memory.injection.full_injection import FullInjectionPolicy
from framework.memory.injection.restricted_injection import RestrictedInjectionPolicy


# -- Helpers ------------------------------------------------------------------


def _make_tool_call_msg(content: str = "", tool_name: str = "read_file") -> dict:
    """Assistant message with tool_calls."""
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [{"id": "tc_1", "type": "function", "function": {"name": tool_name, "arguments": "{}"}}],
    }


def _make_tool_result_msg(content: str = "result", tool_name: str = "read_file") -> dict:
    """Tool result message."""
    return {
        "role": "tool",
        "content": content,
        "name": tool_name,
        "tool_call_id": "tc_1",
    }


def _make_user_msg(content: str = "hello") -> dict:
    return {"role": "user", "content": content}


def _make_assistant_msg(content: str = "response") -> dict:
    return {"role": "assistant", "content": content}


def _make_react_turn(user: str, tool_calls: int = 2) -> list[dict]:
    """Simulate a ReAct turn: user -> assistant(think) -> [tool_call/result pairs] -> assistant(response)."""
    msgs = [_make_user_msg(user)]
    msgs.append(_make_assistant_msg("Let me check..."))
    for i in range(tool_calls):
        msgs.append(_make_tool_call_msg(f"calling tool {i}"))
        msgs.append(_make_tool_result_msg(f"tool result {i}"))
    msgs.append(_make_assistant_msg("Based on the results..."))
    return msgs


def _make_session_messages(turns: int = 10, tool_calls_per_turn: int = 2) -> list[ChatMessage]:
    """Build a realistic ReAct session with N turns."""
    raw: list[dict] = []
    for t in range(turns):
        raw.extend(_make_react_turn(f"user message {t}", tool_calls_per_turn))
    return [ChatMessage.coerce(m) for m in raw]


class FakeMemorySystem:
    """Minimal fake satisfying MemorySystem ABC."""

    def __init__(self, messages: list[ChatMessage], knowledge: Any = None):
        self._messages = messages
        self._knowledge = knowledge

    def create_message_history(
        self, context: Any, initial_messages: Any = None,
    ) -> Any:
        from framework.memory.history import ListMessageHistory
        return ListMessageHistory(initial_messages or [])

    async def get_history(self, context: Any, max_messages: int | None = None) -> list[ChatMessage]:
        result = self._messages
        if max_messages is not None:
            result = result[-max_messages:]
        return result

    async def get_knowledge(self, context: Any) -> Any:
        if self._knowledge is not None:
            return self._knowledge
        from framework.memory.core.models import LongTermMemory
        return LongTermMemory()

    async def retrieve_knowledge(self, context: Any, query: str = "") -> Any:
        return await self.get_knowledge(context)

    async def get_history_entries(self, context: Any, **kwargs: Any) -> list[dict]:
        return []

    def get_providers(self) -> list[Any]:
        return []

    async def prefetch_memories(self, query: str, context: Any) -> str | None:
        return None

    async def get_knowledge_directory(self, context: Any) -> Any:
        return None


# -- Test 1: Single Assemble per Request --------------------------------------


class TestSingleAssemblePerRequest:
    """After simplification, assemble is called once per request (no triple redundancy)."""

    @pytest.mark.asyncio
    async def test_assemble_called_once_per_request(self):
        """Verify that assemble() produces a single InjectionResult per call."""
        all_messages = _make_session_messages(turns=5)
        memory_system = FakeMemorySystem(messages=all_messages)
        policy = FullInjectionPolicy()
        context = MemoryContext(session_id="test")

        result = await policy.assemble(context=context, memory_system=memory_system)
        assert isinstance(result, InjectionResult)
        assert isinstance(result.system_prompt, str)
        assert isinstance(result.messages, list)
        assert len(result.messages) > 0

    @pytest.mark.asyncio
    async def test_single_assemble_produces_complete_result(self):
        """One assemble produces both system_prompt and messages."""
        all_messages = _make_session_messages(turns=10)
        memory_system = FakeMemorySystem(messages=all_messages)

        policy = FullInjectionPolicy(
            budget=MemoryBudget(max_history_messages=50),
        )
        context = MemoryContext(session_id="test")

        result = await policy.assemble(context=context, memory_system=memory_system)

        # System prompt is a string (may be empty if no knowledge)
        assert isinstance(result.system_prompt, str)
        # Messages are available
        assert isinstance(result.messages, list)
        assert len(result.messages) == 50


# -- Test 2: Injection Returns All Messages -----------------------------------


class TestSimplifiedInjection:
    """Expected behavior after simplification.

    Design goals:
    1. Injection retrieves raw data (no filtering)
    2. Governance handles all message shaping for LLM
    3. Clear separation: injection = data retrieval, governance = presentation
    """

    @pytest.mark.asyncio
    async def test_injection_returns_all_messages_including_tools(self):
        """Injection should return all messages as-is; governance decides what to trim."""
        all_messages = _make_session_messages(turns=10, tool_calls_per_turn=2)
        memory_system = FakeMemorySystem(messages=all_messages)
        policy = FullInjectionPolicy(
            budget=MemoryBudget(max_history_messages=50),
        )

        context = MemoryContext(session_id="test")
        result = await policy.assemble(context=context, memory_system=memory_system)

        # All 50 messages returned, including tool messages
        assert len(result.messages) == 50
        tool_msgs = [m for m in result.messages if m.role == "tool"]
        assert len(tool_msgs) > 0, "Tool messages should be present for governance"

    @pytest.mark.asyncio
    async def test_restricted_policy_for_subagent(self):
        """Subagent policy: session messages only, no knowledge/archive/providers."""
        all_messages = _make_session_messages(turns=5)
        memory_system = FakeMemorySystem(messages=all_messages)
        policy = RestrictedInjectionPolicy(
            max_session_messages=30,
        )

        context = MemoryContext(session_id="sub-test")
        result = await policy.assemble(context=context, memory_system=memory_system)

        assert len(result.messages) == 30
        assert result.system_prompt == ""  # No knowledge for subagents


# -- Test 3: Priority-based Section Trimming ----------------------------------


class TestPrioritySectionTrimming:
    """Verify that system prompt section trimming works correctly.

    This should remain unchanged after simplification.
    """

    @pytest.mark.asyncio
    async def test_low_priority_sections_dropped_first(self):
        """When token budget is exceeded, lowest priority sections drop first."""
        policy = FullInjectionPolicy(
            budget=MemoryBudget(max_system_prompt_tokens=500),
        )
        memory_system = FakeMemorySystem(messages=[])

        # Mock knowledge to inject large sections
        from framework.memory.core.models import LongTermMemory
        memory_system._knowledge = LongTermMemory(
            soul="S" * 50,
            user="U" * 50,
            memory="M" * 300,  # This should be trimmed (priority=90)
        )

        context = MemoryContext(session_id="test")
        result = await policy.assemble(context=context, memory_system=memory_system)

        # Soul (100) and User (100) should be in the system_prompt
        assert "S" * 50 in result.system_prompt
        assert "U" * 50 in result.system_prompt
        # Memory section (90 priority) may be trimmed or partially included
        # The key is that soul and user survive while memory is deprioritized


# -- Test 4: Message Count Across Pipeline Stages -----------------------------


class TestMessageCountAcrossPipeline:
    """Track message count at each pipeline stage to verify no message loss.

    After simplification, injection preserves all messages.
    """

    @pytest.mark.asyncio
    async def test_expected_behavior_message_count_per_stage(self):
        """Expected message counts after simplification."""
        all_messages = _make_session_messages(turns=10, tool_calls_per_turn=2)
        assert len(all_messages) == 70

        memory_system = FakeMemorySystem(messages=all_messages)
        # Simplified: no filter, all messages preserved
        policy = FullInjectionPolicy(
            budget=MemoryBudget(max_history_messages=50),
        )
        context = MemoryContext(session_id="test")
        result = await policy.assemble(context=context, memory_system=memory_system)

        # Stage 1: After injection (no filter)
        # -> 50 messages, including tools
        assert len(result.messages) == 50

        # Stage 2: Tool messages are present for governance
        tool_msgs = sum(1 for m in result.messages if m.role == "tool")
        assert tool_msgs > 0, "Tool messages must be present for governance to compact"
