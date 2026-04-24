"""Tests for MemoryCompactionPipeline, MessageCompactionPolicy, and BoundaryPolicy."""

import pytest

from framework.memory.compaction.policy import (
    ConservativeCompactionPolicy,
    KeepAllCompactionPolicy,
    MessageCompactionDecision,
)
from framework.memory.compaction.pipeline import MemoryCompactionPipeline
from framework.memory.compaction.boundary import ToolChainBoundaryPolicy
from framework.memory.compaction.boundary import BoundaryPolicy
from framework.memory.core.message import ChatMessage
from framework.memory.core.scope import MemoryContext


class TestMessageCompactionPolicy:
    def test_conservative_user_is_summarize(self):
        policy = ConservativeCompactionPolicy()
        msg = ChatMessage(role="user", content="hello")
        assert policy.decide(msg, MemoryContext(), "idle_compact") == MessageCompactionDecision.SUMMARIZE

    def test_conservative_plain_assistant_is_summarize(self):
        policy = ConservativeCompactionPolicy()
        msg = ChatMessage(role="assistant", content="hi")
        assert policy.decide(msg, MemoryContext(), "idle_compact") == MessageCompactionDecision.SUMMARIZE

    def test_conservative_assistant_with_tool_calls_is_keep_raw(self):
        policy = ConservativeCompactionPolicy()
        msg = ChatMessage(
            role="assistant",
            content="",
            tool_calls=[{"id": "tc1", "type": "function", "function": {"name": "search"}}],
        )
        assert policy.decide(msg, MemoryContext(), "idle_compact") == MessageCompactionDecision.KEEP_RAW

    def test_conservative_tool_is_drop_from_summary(self):
        policy = ConservativeCompactionPolicy()
        msg = ChatMessage(role="tool", content="result", tool_call_id="tc1")
        assert policy.decide(msg, MemoryContext(), "idle_compact") == MessageCompactionDecision.DROP_FROM_SUMMARY

    def test_conservative_high_value_tool_is_summarize(self):
        policy = ConservativeCompactionPolicy(high_value_tools={"web_search"})
        msg = ChatMessage(role="tool", content="search result", tool_call_id="tc1", name="web_search")
        assert policy.decide(msg, MemoryContext(), "idle_compact") == MessageCompactionDecision.SUMMARIZE

    def test_conservative_system_is_keep_raw(self):
        policy = ConservativeCompactionPolicy()
        msg = ChatMessage(role="system", content="sys prompt")
        assert policy.decide(msg, MemoryContext(), "idle_compact") == MessageCompactionDecision.KEEP_RAW

    def test_keep_all_is_keep_raw(self):
        policy = KeepAllCompactionPolicy()
        for role in ("user", "assistant", "tool", "system"):
            msg = ChatMessage(role=role, content="x")
            assert policy.decide(msg, MemoryContext(), "idle_compact") == MessageCompactionDecision.KEEP_RAW

    def test_decide_all_returns_same_length(self):
        policy = ConservativeCompactionPolicy()
        messages = [
            ChatMessage(role="user", content="a"),
            ChatMessage(role="assistant", content="b", tool_calls=[{"id": "tc1"}]),
            ChatMessage(role="tool", content="c", tool_call_id="tc1"),
        ]
        decisions = policy.decide_all(messages, MemoryContext(), "test")
        assert len(decisions) == len(messages)


class TestToolChainBoundaryPolicy:
    def test_basic_truncation(self):
        policy = ToolChainBoundaryPolicy()
        messages = [
            ChatMessage(role="user", content="0"),
            ChatMessage(role="user", content="1"),
            ChatMessage(role="user", content="2"),
        ]
        decisions = [MessageCompactionDecision.SUMMARIZE] * 3
        boundary = policy.find_prune_boundary(messages, decisions, target_prune_count=1)
        assert boundary == 1

    def test_protects_tool_call_chain(self):
        """If truncation would split assistant+tool, boundary expands to cover the chain."""
        policy = ToolChainBoundaryPolicy()
        messages = [
            ChatMessage(role="user", content="0"),
            ChatMessage(role="assistant", content="", tool_calls=[{"id": "tc1"}]),
            ChatMessage(role="tool", content="result", tool_call_id="tc1"),
            ChatMessage(role="user", content="3"),
        ]
        decisions = [MessageCompactionDecision.SUMMARIZE] * 4
        # target_prune_count=3 would cut after msg[2], but msg[1] and msg[2] form a chain
        boundary = policy.find_prune_boundary(messages, decisions, target_prune_count=3)
        # Boundary should extend past the chain end (index 2), so boundary == 4 (keep all)
        # or at least not cut between 1 and 2
        assert boundary <= 1 or boundary >= 3  # never between 1 and 2

    def test_keeps_raw_messages(self):
        """KEEP_RAW messages must not be pruned."""
        policy = ToolChainBoundaryPolicy()
        messages = [
            ChatMessage(role="user", content="0"),
            ChatMessage(role="user", content="1"),
            ChatMessage(role="user", content="2"),
        ]
        decisions = [
            MessageCompactionDecision.SUMMARIZE,
            MessageCompactionDecision.KEEP_RAW,
            MessageCompactionDecision.SUMMARIZE,
        ]
        boundary = policy.find_prune_boundary(messages, decisions, target_prune_count=2)
        # msg[1] is KEEP_RAW, so boundary must shrink to before it (boundary <= 1)
        assert boundary <= 1

    def test_can_keep_more_than_target(self):
        """Boundary safety can force keeping more tail messages than target_prune_count."""
        policy = ToolChainBoundaryPolicy()
        messages = [
            ChatMessage(role="user", content="0"),
            ChatMessage(role="assistant", content="", tool_calls=[{"id": "tc1"}]),
            ChatMessage(role="tool", content="r1", tool_call_id="tc1"),
            ChatMessage(role="user", content="3"),
        ]
        decisions = [MessageCompactionDecision.SUMMARIZE] * 4
        # target=2 would cut between assistant(1) and tool result(2) — unsafe.
        # Boundary shrinks to before the chain start, so boundary <= 1.
        boundary = policy.find_prune_boundary(messages, decisions, target_prune_count=2)
        assert boundary <= 1
        remaining = len(messages) - boundary
        assert remaining >= 2  # we kept more than the nominal keep_recent

    def test_respects_min_tail_keep(self):
        policy = ToolChainBoundaryPolicy(min_tail_keep=2)
        messages = [
            ChatMessage(role="user", content="0"),
            ChatMessage(role="user", content="1"),
            ChatMessage(role="user", content="2"),
        ]
        decisions = [MessageCompactionDecision.SUMMARIZE] * 3
        boundary = policy.find_prune_boundary(messages, decisions, target_prune_count=2)
        # min_tail_keep=2 means at least 2 messages must remain, so boundary <= 1
        assert boundary <= 1


class TestMemoryCompactionPipeline:
    @pytest.mark.asyncio
    async def test_pipeline_no_prune_when_under_keep_recent(self):
        pipeline = MemoryCompactionPipeline()
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = await pipeline.run(
            context=MemoryContext(),
            messages=messages,
            reason="idle_compact",
            keep_recent_messages=5,
        )
        assert len(result.remaining_messages) == 2
        assert result.pruned_messages == []
        assert not result.archived

    @pytest.mark.asyncio
    async def test_pipeline_prunes_with_policy_and_boundary(self):
        pipeline = MemoryCompactionPipeline()
        messages = [
            {"role": "user", "content": "0"},
            {"role": "user", "content": "1"},
            {"role": "user", "content": "2"},
            {"role": "user", "content": "3"},
        ]
        result = await pipeline.run(
            context=MemoryContext(),
            messages=messages,
            reason="idle_compact",
            keep_recent_messages=2,
        )
        assert len(result.remaining_messages) == 2
        assert len(result.pruned_messages) == 2
        assert result.remaining_messages[0]["content"] == "2"
        assert result.remaining_messages[1]["content"] == "3"

    @pytest.mark.asyncio
    async def test_pipeline_protects_tool_chain(self):
        pipeline = MemoryCompactionPipeline()
        messages = [
            {"role": "user", "content": "0"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1"}]},
            {"role": "tool", "content": "result", "tool_call_id": "tc1"},
            {"role": "user", "content": "3"},
        ]
        result = await pipeline.run(
            context=MemoryContext(),
            messages=messages,
            reason="idle_compact",
            keep_recent_messages=1,
        )
        # keep_recent=1 would normally keep only msg[3], but boundary protects
        # the assistant+tool chain at indices 1-2, so we keep indices 1-3.
        assert len(result.remaining_messages) >= 2
        # Verify no orphan tool result
        remaining_roles = [m["role"] for m in result.remaining_messages]
        if "tool" in remaining_roles:
            # The assistant that called it must also be present
            assert "assistant" in remaining_roles

    @pytest.mark.asyncio
    async def test_pipeline_classifies_messages(self):
        pipeline = MemoryCompactionPipeline()
        messages = [
            {"role": "user", "content": "q"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a", "tool_calls": [{"id": "tc1"}]},
            {"role": "tool", "content": "r", "tool_call_id": "tc1"},
        ]
        result = await pipeline.run(
            context=MemoryContext(),
            messages=messages,
            reason="idle_compact",
            keep_recent_messages=2,
        )
        # user messages should be SUMMARIZE, assistant-with-tool_calls KEEP_RAW, tool DROP
        assert any(m["role"] == "user" for m in result.summarized_messages)
        # assistant is KEEP_RAW so it stays in remaining, not pruned
        assert any(m["role"] == "assistant" for m in result.remaining_messages)
        # tool is DROP_FROM_SUMMARY but follows its KEEP_RAW assistant, so also remaining
        assert any(m["role"] == "tool" for m in result.remaining_messages)

    @pytest.mark.asyncio
    async def test_pipeline_generates_summary(self):
        from framework.memory.compaction.pipeline import HeuristicSummaryStrategy

        pipeline = MemoryCompactionPipeline(summary_strategy=HeuristicSummaryStrategy())
        messages = [
            {"role": "user", "content": "hello world"},
            {"role": "user", "content": "how are you"},
            {"role": "assistant", "content": "fine"},
        ]
        result = await pipeline.run(
            context=MemoryContext(),
            messages=messages,
            reason="idle_compact",
            keep_recent_messages=1,
        )
        assert result.summary is not None
        assert "hello world" in result.summary or "how are you" in result.summary
