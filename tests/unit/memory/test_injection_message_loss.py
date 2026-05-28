"""TDD tests for the message-loss bug: 70 stored messages → <10 reaching LLM.

Root cause: ToolMessageFilterStrategy strips ALL tool_call + tool_result messages
during injection, before governance runs. In a ReAct agent, each tool-using turn
produces 2 messages (assistant with tool_calls + tool result). With 30 tool-using
turns in 70 messages, filtering removes 60 messages, leaving only ~10 pure
user/assistant messages.

Fix: Default filter should be NoopFilterStrategy (pass-through). Governance
handles tool message compaction via MicrocompactGovernance and structural
repair via ToolChainRepairGovernance.
"""
from __future__ import annotations

import pytest

from framework.memory.core.message import ChatMessage
from framework.memory.core.models import MemoryBudget
from framework.memory.core.scope import MemoryContext
from framework.memory.default_system import DefaultMemorySystem
from framework.memory.injection import FullInjectionPolicy
from framework.memory.injection.filter import (
    NoopFilterStrategy,
    ToolMessageFilterStrategy,
)
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.registry.in_memory import InMemoryStoreRegistry


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_ctx(session_id: str = "test") -> MemoryContext:
    return MemoryContext(session_id=session_id, user_id="u1")


def _create_system(
    max_messages: int = 100,
) -> DefaultMemorySystem:
    registry = InMemoryStoreRegistry()
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    cleanup_config: dict[str, int | float] = {
        "max_messages": max_messages,
        "keep_ratio": 0.4,
    }
    return DefaultMemorySystem(
        layer_set=layer_set,
        store_registry=registry,
        cleanup_config=cleanup_config,
    )


def _build_react_conversation(
    num_tool_turns: int = 15,
    num_plain_turns: int = 5,
) -> list[dict]:
    """Build a realistic ReAct conversation with tool calls.

    Each tool turn = user + assistant(tool_calls) + tool(result) + assistant(summary) = 4 msgs
    Each plain turn = user + assistant = 2 msgs

    Total = num_tool_turns * 4 + num_plain_turns * 2
    """
    messages: list[dict] = []
    tc_id = 0

    for i in range(num_plain_turns):
        messages.append({"role": "user", "content": f"plain question {i}"})
        messages.append({"role": "assistant", "content": f"plain answer {i}"})

    for i in range(num_tool_turns):
        messages.append({"role": "user", "content": f"tool question {i}"})
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": f"tc_{tc_id}",
                "type": "function",
                "function": {"name": "read_file", "arguments": f'{{"path": "file_{i}.txt"}}'},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": f"tc_{tc_id}",
            "name": "read_file",
            "content": f"file content {i}: " + "x" * 200,
        })
        messages.append({"role": "assistant", "content": f"tool answer {i}: found data"})
        tc_id += 1

    return messages


# ── TDD: Bug Reproduction Tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_bug_reproduction_tool_filter_causes_massive_message_loss():
    """BUG: ToolMessageFilterStrategy removes most messages in tool-heavy conversations.

    This test documents the exact bug:
    - 15 tool turns × 4 msgs + 5 plain turns × 2 msgs = 70 total messages
    - ToolMessageFilterStrategy removes: 15 assistant(tool_calls) + 15 tool(results) = 30 msgs
    - After cleanup (keep_ratio=0.4): further reduced
    - Result: only ~10 messages survive instead of ~70

    This test SHOULD FAIL with current code (demonstrating the bug)
    and PASS after fix (using NoopFilterStrategy as default).
    """
    system = _create_system(max_messages=200)
    await system.initialize()
    ctx = _make_ctx("bug-repro")

    # Seed 70 messages: 15 tool turns + 5 plain turns
    messages = _build_react_conversation(num_tool_turns=15, num_plain_turns=5)
    assert len(messages) == 70, f"Expected 70 messages, got {len(messages)}"

    await system._layers.session.add_messages(ctx, messages)

    # Verify all 70 are stored
    stored = await system._layers.session.get_all_messages(ctx)
    assert len(stored) == 70, f"Storage should have 70 messages, got {len(stored)}"

    # Now inject using FullInjectionPolicy (current default = ToolMessageFilterStrategy)
    bundle = await FullInjectionPolicy().assemble(
        context=ctx, memory_system=system, query="",
    )

    # BUG: bundle.messages should have ~70 messages (limited by budget)
    # but actually has <10 because ToolMessageFilterStrategy removed all tool msgs
    injected_count = len(bundle.messages)

    # With the fix (NoopFilterStrategy), tool messages should be preserved
    # Governance handles compaction, not injection filter
    assert injected_count >= 30, (
        f"Expected >= 30 messages after injection (tool msgs preserved for governance), "
        f"got {injected_count}. ToolMessageFilterStrategy is removing too many messages."
    )


@pytest.mark.asyncio
async def test_noop_filter_preserves_all_messages_for_governance():
    """NoopFilterStrategy keeps all messages so governance can handle them properly.

    Governance chain (LossyCompaction → ToolChainRepair → TokenBudget) is the
    RIGHT place to manage message count and structure. Injection should NOT filter.
    """
    system = _create_system(max_messages=200)
    await system.initialize()
    ctx = _make_ctx("noop-filter")

    messages = _build_react_conversation(num_tool_turns=15, num_plain_turns=5)
    await system._layers.session.add_messages(ctx, messages)

    bundle = await FullInjectionPolicy(
        filter_strategy=NoopFilterStrategy(),
    ).assemble(
        context=ctx, memory_system=system, query="",
    )

    # Budget limits to max_history_messages=50, but NoopFilter doesn't drop more
    injected_count = len(bundle.messages)
    assert injected_count == 50, (
        f"NoopFilter should preserve all budget-limited messages (50), got {injected_count}"
    )

    # Verify tool messages are present in the kept window
    tool_call_count = sum(1 for m in bundle.messages if m.tool_calls)
    tool_result_count = sum(
        1 for m in bundle.messages
        if m.role and str(m.role) == "tool"
    )
    assert tool_call_count >= 5, f"Expected >= 5 tool_call msgs, got {tool_call_count}"
    assert tool_result_count >= 5, f"Expected >= 5 tool_result msgs, got {tool_result_count}"


@pytest.mark.asyncio
async def test_tool_filter_vs_noop_filter_comparison():
    """Side-by-side comparison showing ToolMessageFilterStrategy loss."""
    system = _create_system(max_messages=200)
    await system.initialize()
    ctx = _make_ctx("comparison")

    messages = _build_react_conversation(num_tool_turns=10, num_plain_turns=5)
    await system._layers.session.add_messages(ctx, messages)
    # Total = 10*4 + 5*2 = 50 messages

    # With ToolMessageFilterStrategy (current broken default)
    bundle_filtered = await FullInjectionPolicy(
        filter_strategy=ToolMessageFilterStrategy(),
    ).assemble(context=ctx, memory_system=system, query="")

    # With NoopFilterStrategy (correct default)
    bundle_noop = await FullInjectionPolicy(
        filter_strategy=NoopFilterStrategy(),
    ).assemble(context=ctx, memory_system=system, query="")

    filtered_count = len(bundle_filtered.messages)
    noop_count = len(bundle_noop.messages)

    assert noop_count == 50, f"NoopFilter should keep all 50 budget-limited, got {noop_count}"
    assert filtered_count < 35, f"ToolFilter should remove tool msgs, got {filtered_count}"
    assert noop_count > filtered_count, (
        f"NoopFilter should preserve more messages: {noop_count} vs {filtered_count}"
    )


@pytest.mark.asyncio
async def test_cleanup_does_not_compound_with_filter_loss():
    """Verify that cleanup + filter don't compound to devastating message loss."""
    system = _create_system(max_messages=30)  # Low threshold triggers cleanup
    await system.initialize()
    ctx = _make_ctx("compound")

    # Create 50 messages - triggers cleanup at 30, keeps 12 (keep_ratio=0.4)
    messages = _build_react_conversation(num_tool_turns=10, num_plain_turns=5)
    # Total: 50 messages

    history = system.create_message_history(ctx)
    for msg in messages:
        await history.append(msg)

    # After cleanup, some messages were pruned
    stored = await system._layers.session.get_all_messages(ctx)
    stored_count = len(stored)

    # Cleanup should have reduced count
    assert stored_count <= 30, f"Cleanup should reduce to <=30, got {stored_count}"

    # With NoopFilter: injected count should match stored count (governance handles rest)
    bundle_noop = await FullInjectionPolicy(
        filter_strategy=NoopFilterStrategy(),
    ).assemble(context=ctx, memory_system=system, query="")

    # Without cleanup interference, injection should preserve all stored messages
    assert len(bundle_noop.messages) == stored_count, (
        f"NoopFilter should keep all {stored_count} stored messages, "
        f"got {len(bundle_noop.messages)}"
    )


@pytest.mark.asyncio
async def test_default_filter_is_noop():
    """FullInjectionPolicy default filter should be NoopFilterStrategy after fix."""
    policy = FullInjectionPolicy()
    assert isinstance(policy._filter, NoopFilterStrategy), (
        f"Default filter should be NoopFilterStrategy, got {type(policy._filter).__name__}. "
        f"ToolMessageFilterStrategy causes message loss in tool-heavy conversations."
    )
