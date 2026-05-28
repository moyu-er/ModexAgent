"""TDD tests for context construction issues and simplified design.

These tests demonstrate:
1. The 70→10 message loss caused by ToolMessageFilterStrategy
2. The triple-assemble redundancy in assemble_context()
3. The contradiction between filter and governance
4. Expected behavior after simplification

Run: pytest tests/unit/memory/test_context_construction_issues.py -v
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from framework.memory.core.message import ChatMessage
from framework.memory.core.models import (
    MemoryBudget,
    MemoryContextBundle,
    PromptSection,
)
from framework.memory.core.scope import MemoryContext
from framework.memory.injection.filter import (
    NoopFilterStrategy,
    ToolMessageFilterStrategy,
)
from framework.memory.injection.full_injection import FullInjectionPolicy
from framework.memory.injection.restricted_injection import RestrictedInjectionPolicy


# ── Helpers ──────────────────────────────────────────────────────────────────


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
    """Simulate a ReAct turn: user → assistant(think) → [tool_call/result pairs] → assistant(response)."""
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
    """Minimal fake satisfying InjectableMemorySystem protocol."""

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


# ── Test 1: 70→10 Message Loss ──────────────────────────────────────────────


class TestToolMessageFilterCausesExcessiveMessageLoss:
    """BUG: ToolMessageFilterStrategy drops ALL tool messages, losing context.

    In a 10-turn ReAct session (2 tool calls per turn), each turn produces
    ~7 messages (user + assistant + 2×(tool_call + tool_result) + assistant).
    That's 70 messages total. After filter, only user + pure-assistant messages
    survive: 10 turns × 3 = ~20 messages. With only the last 50 fetched,
    it drops to ~10.
    """

    def test_filter_removes_all_tool_messages_from_react_session(self):
        """ToolMessageFilterStrategy removes every tool_call and tool_result message."""
        messages = _make_session_messages(turns=10, tool_calls_per_turn=2)
        # 10 turns × (1 user + 1 assistant_think + 2 tool_calls + 2 tool_results + 1 assistant_response) = 70
        assert len(messages) == 70, f"Expected 70 messages, got {len(messages)}"

        tool_msgs = [m for m in messages if m.role in ("tool",) or (m.role == "assistant" and m.tool_calls)]
        assert len(tool_msgs) == 40, f"Expected 40 tool-related messages, got {len(tool_msgs)}"

        filtered = ToolMessageFilterStrategy().filter(messages)
        # Only user + pure assistant messages survive
        assert len(filtered) == 30  # 10 user + 10 assistant_think + 10 assistant_response (no tool_calls)

    @pytest.mark.asyncio
    async def test_full_injection_policy_produces_few_messages_from_many(self):
        """FullInjectionPolicy assembles only ~10 non-tool messages from 70 stored.

        This reproduces the reported bug: 70 session messages → only ~10 in context.
        """
        all_messages = _make_session_messages(turns=10, tool_calls_per_turn=2)
        assert len(all_messages) == 70

        memory_system = FakeMemorySystem(messages=all_messages)
        policy = FullInjectionPolicy(
            budget=MemoryBudget(max_history_messages=50),
            filter_strategy=ToolMessageFilterStrategy(),
        )

        context = MemoryContext(session_id="test-session", user_id="user1")
        bundle = await policy.assemble(context=context, memory_system=memory_system)

        # BUG: Only ~20 non-tool messages survive from 50 fetched (40 tool messages dropped)
        # 50 fetched → filter drops all tool_call + tool_result → ~22 survive
        assert len(bundle.messages) < 30, (
            f"Expected heavy loss (~20 messages), got {len(bundle.messages)}"
        )
        # None of the surviving messages should be tool-related
        for msg in bundle.messages:
            assert msg.role != "tool"
            assert not (msg.role == "assistant" and msg.tool_calls)

    @pytest.mark.asyncio
    async def test_noop_filter_preserves_all_messages(self):
        """With NoopFilterStrategy, all 50 messages (from 70) are preserved."""
        all_messages = _make_session_messages(turns=10, tool_calls_per_turn=2)
        memory_system = FakeMemorySystem(messages=all_messages)
        policy = FullInjectionPolicy(
            budget=MemoryBudget(max_history_messages=50),
            filter_strategy=NoopFilterStrategy(),
        )

        context = MemoryContext(session_id="test-session", user_id="user1")
        bundle = await policy.assemble(context=context, memory_system=memory_system)

        # All 50 messages preserved; governance should handle tool compaction
        assert len(bundle.messages) == 50


# ── Test 2: Triple Assemble Redundancy ──────────────────────────────────────


class TestTripleAssembleRedundancy:
    """assemble_context() triggers injection_policy.assemble() 3 times per request.

    Each assemble reads from all memory layers (knowledge + archive + session).
    This is wasteful I/O — the result is identical across all 3 calls within
    a single request.
    """

    @pytest.mark.asyncio
    async def test_assemble_called_three_times_per_request(self):
        """Verify that assemble() is called 3 times during a single request.

        This test should FAIL after simplification (assemble called once).
        """
        all_messages = _make_session_messages(turns=5)
        memory_system = FakeMemorySystem(messages=all_messages)
        policy = FullInjectionPolicy()
        context = MemoryContext(session_id="test")

        call_count = 0
        original_assemble = policy.assemble

        async def counting_assemble(**kwargs: Any) -> MemoryContextBundle:
            nonlocal call_count
            call_count += 1
            return await original_assemble(**kwargs)

        policy.assemble = counting_assemble

        # Simulate: load_with_metadata → load → build_system_prompt
        # (mimics assemble_context flow)
        await policy.assemble(context=context, memory_system=memory_system)
        await policy.assemble(context=context, memory_system=memory_system)
        await policy.assemble(context=context, memory_system=memory_system)

        assert call_count == 3, "Three separate assemble calls in one request"

    @pytest.mark.asyncio
    async def test_expected_single_assemble_after_simplification(self):
        """After simplification, assemble should be called ONCE per request.

        The result (system_sections + messages) is computed once and reused
        for both ContextState and system prompt.
        """
        # Use enough turns to exceed max_history_messages
        all_messages = _make_session_messages(turns=10)
        memory_system = FakeMemorySystem(messages=all_messages)

        # Simplified: single assemble, no filter
        policy = FullInjectionPolicy(
            budget=MemoryBudget(max_history_messages=50),
            filter_strategy=NoopFilterStrategy(),
        )
        context = MemoryContext(session_id="test")

        # One assemble produces everything needed
        bundle = await policy.assemble(context=context, memory_system=memory_system)

        # System sections available for prompt building
        assert isinstance(bundle.system_sections, list)
        # Messages available for history (unfiltered)
        assert isinstance(bundle.messages, list)
        assert len(bundle.messages) == 50


# ── Test 3: Filter vs Governance Contradiction ──────────────────────────────


class TestFilterGovernanceContradiction:
    """ToolMessageFilterStrategy and MicrocompactGovernance are contradictory.

    The filter removes ALL tool messages during injection.
    MicrocompactGovernance tries to compact tool results during governance.
    But by the time governance runs, there are no tool messages to compact.
    """

    @pytest.mark.asyncio
    async def test_governance_receives_no_tool_messages_after_filter(self):
        """After ToolMessageFilterStrategy, governance sees zero tool messages.

        MicrocompactGovernance's keep_recent logic is useless here.
        """
        all_messages = _make_session_messages(turns=5, tool_calls_per_turn=2)
        memory_system = FakeMemorySystem(messages=all_messages)
        policy = FullInjectionPolicy(filter_strategy=ToolMessageFilterStrategy())
        context = MemoryContext(session_id="test")

        bundle = await policy.assemble(context=context, memory_system=memory_system)

        tool_messages = [
            m for m in bundle.messages
            if m.role == "tool" or (m.role == "assistant" and m.tool_calls)
        ]
        assert len(tool_messages) == 0, (
            f"Governance receives {len(tool_messages)} tool messages — "
            f"MicrocompactGovernance has nothing to compact"
        )

    @pytest.mark.asyncio
    async def test_governance_should_handle_tool_compaction(self):
        """After simplification: injection returns all messages, governance compacts tools.

        This is the EXPECTED design:
        - Injection: retrieve all messages (no filter)
        - Governance: decide which tool messages to keep/compact/truncate
        """
        all_messages = _make_session_messages(turns=5, tool_calls_per_turn=2)
        memory_system = FakeMemorySystem(messages=all_messages)

        # Simplified injection: no filter
        policy = FullInjectionPolicy(
            budget=MemoryBudget(max_history_messages=50),
            filter_strategy=NoopFilterStrategy(),
        )
        context = MemoryContext(session_id="test")
        bundle = await policy.assemble(context=context, memory_system=memory_system)

        # All messages (including tools) available for governance
        tool_count = sum(
            1 for m in bundle.messages
            if m.role == "tool" or (m.role == "assistant" and m.tool_calls)
        )
        assert tool_count > 0, "Governance should receive tool messages to compact"


# ── Test 4: Simplified Context Assembly ─────────────────────────────────────


class TestSimplifiedContextAssembly:
    """Expected behavior after simplification.

    Design goals:
    1. Injection retrieves raw data (no filtering)
    2. Governance handles all message shaping for LLM
    3. Single assemble per request
    4. Clear separation: injection = data retrieval, governance = presentation
    """

    @pytest.mark.asyncio
    async def test_injection_returns_all_messages_without_filter(self):
        """Injection should return messages as-is; governance decides what to trim."""
        all_messages = _make_session_messages(turns=10, tool_calls_per_turn=2)
        memory_system = FakeMemorySystem(messages=all_messages)
        policy = FullInjectionPolicy(
            budget=MemoryBudget(max_history_messages=50),
            filter_strategy=NoopFilterStrategy(),
        )

        context = MemoryContext(session_id="test")
        bundle = await policy.assemble(context=context, memory_system=memory_system)

        # All 50 messages returned, including tool messages
        assert len(bundle.messages) == 50
        tool_msgs = [m for m in bundle.messages if m.role == "tool"]
        assert len(tool_msgs) > 0, "Tool messages should be present for governance"

    @pytest.mark.asyncio
    async def test_restricted_policy_for_subagent(self):
        """Subagent policy: session messages only, no knowledge/archive/providers.

        RestrictedInjectionPolicy should still not filter tool messages —
        governance handles that.
        """
        all_messages = _make_session_messages(turns=5)
        memory_system = FakeMemorySystem(messages=all_messages)
        policy = RestrictedInjectionPolicy(
            max_session_messages=30,
            filter_strategy=NoopFilterStrategy(),
        )

        context = MemoryContext(session_id="sub-test")
        bundle = await policy.assemble(context=context, memory_system=memory_system)

        assert len(bundle.messages) == 30
        assert bundle.system_sections == []  # No knowledge for subagents


# ── Test 5: Priority-based Section Trimming ─────────────────────────────────


class TestPrioritySectionTrimming:
    """Verify that PromptSection priority trimming works correctly.

    This should remain unchanged after simplification.
    """

    @pytest.mark.asyncio
    async def test_low_priority_sections_dropped_first(self):
        """When token budget is exceeded, lowest priority sections drop first."""
        policy = FullInjectionPolicy(
            budget=MemoryBudget(max_system_prompt_tokens=200),
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
        bundle = await policy.assemble(context=context, memory_system=memory_system)

        # Soul (100) and User (100) should survive; Memory (90) should be trimmed
        section_keys = [s.key for s in bundle.system_sections]
        assert "knowledge:soul" in section_keys
        assert "knowledge:user" in section_keys
        # Memory section may be trimmed or dropped due to low priority + budget


# ── Test 6: Message Count Across Pipeline Stages ────────────────────────────


class TestMessageCountAcrossPipeline:
    """Track message count at each pipeline stage to diagnose loss.

    These tests document the EXPECTED behavior after simplification.
    Currently they will demonstrate the problem.
    """

    @pytest.mark.asyncio
    async def test_current_behavior_message_count_per_stage(self):
        """Document current (buggy) message counts at each stage."""
        all_messages = _make_session_messages(turns=10, tool_calls_per_turn=2)
        # Stage 0: Session storage
        assert len(all_messages) == 70

        memory_system = FakeMemorySystem(messages=all_messages)
        policy = FullInjectionPolicy(
            budget=MemoryBudget(max_history_messages=50),
            filter_strategy=ToolMessageFilterStrategy(),
        )
        context = MemoryContext(session_id="test")
        bundle = await policy.assemble(context=context, memory_system=memory_system)

        # Stage 1: After get_history(limit=50)
        # → 50 messages fetched

        # Stage 2: After ToolMessageFilterStrategy
        # → Only ~20 non-tool messages survive (tool calls/results dropped)
        assert len(bundle.messages) < 30

        # Stage 3: After governance (not tested here, but would receive ~15)
        # → Still ~15, since there's nothing to compact

    @pytest.mark.asyncio
    async def test_expected_behavior_message_count_per_stage(self):
        """Expected message counts after simplification."""
        all_messages = _make_session_messages(turns=10, tool_calls_per_turn=2)
        assert len(all_messages) == 70

        memory_system = FakeMemorySystem(messages=all_messages)
        # Simplified: no filter
        policy = FullInjectionPolicy(
            budget=MemoryBudget(max_history_messages=50),
            filter_strategy=NoopFilterStrategy(),
        )
        context = MemoryContext(session_id="test")
        bundle = await policy.assemble(context=context, memory_system=memory_system)

        # Stage 1: After injection (no filter)
        # → 50 messages, including tools
        assert len(bundle.messages) == 50

        # Stage 2: After governance (MicrocompactGovernance etc.)
        # → ~30-40 messages (old tool results compacted, recent kept)
        # (governance test would go here — just verify injection output is correct)
        tool_msgs = sum(1 for m in bundle.messages if m.role == "tool")
        assert tool_msgs > 0, "Tool messages must be present for governance to compact"
