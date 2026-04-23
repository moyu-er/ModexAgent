"""Tests for compression strategies."""

import pytest

from framework.memory.compression import (
    HybridCompressionStrategy,
    TokenWindowStrategy,
    ToolChainAwareStrategy,
    TruncationStrategy,
)
from framework.memory.core.compression import CompressionContext


@pytest.mark.asyncio
class TestTruncationStrategy:
    async def test_no_pruning_when_under_limit(self):
        strategy = TruncationStrategy(target_count=10)
        messages = [{"role": "user", "content": str(i)} for i in range(5)]
        result = await strategy.compress(messages, CompressionContext())
        assert not result.pruned_messages

    async def test_prunes_excess_from_head(self):
        strategy = TruncationStrategy(target_count=3)
        messages = [{"role": "user", "content": str(i)} for i in range(5)]
        result = await strategy.compress(messages, CompressionContext())
        assert len(result.pruned_messages) == 2
        assert result.pruned_messages[0]["content"] == "0"
        assert result.pruned_messages[1]["content"] == "1"

    async def test_does_not_split_tool_chain(self):
        strategy = TruncationStrategy(target_count=2)
        messages = [
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "function": {"name": "calc"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "42"},
        ]
        result = await strategy.compress(messages, CompressionContext())
        kept = [m for m in messages if m not in result.pruned_messages]
        call_in_kept = any(m.get("role") == "assistant" and m.get("tool_calls") for m in kept)
        result_in_kept = any(m.get("role") == "tool" for m in kept)
        # 要么都保留，要么都移除
        assert call_in_kept == result_in_kept


@pytest.mark.asyncio
class TestTokenWindowStrategy:
    async def test_no_pruning_when_under_token_limit(self):
        strategy = TokenWindowStrategy(max_tokens=4000)
        messages = [{"role": "user", "content": "hi"}]
        result = await strategy.compress(messages, CompressionContext())
        assert not result.pruned_messages

    async def test_prunes_from_head_to_fit_tokens(self):
        strategy = TokenWindowStrategy(max_tokens=30)
        # ~25 tokens each
        messages = [
            {"role": "user", "content": "a" * 100},
            {"role": "user", "content": "b" * 100},
            {"role": "user", "content": "c" * 100},
        ]
        result = await strategy.compress(messages, CompressionContext())
        assert len(result.pruned_messages) >= 1
        # Latest message should be preserved
        assert messages[-1] not in result.pruned_messages

    async def test_does_not_split_tool_chain(self):
        strategy = TokenWindowStrategy(max_tokens=1)
        messages = [
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "function": {"name": "calc"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "42"},
        ]
        result = await strategy.compress(messages, CompressionContext())
        kept = [m for m in messages if m not in result.pruned_messages]
        call_in_kept = any(m.get("role") == "assistant" and m.get("tool_calls") for m in kept)
        result_in_kept = any(m.get("role") == "tool" for m in kept)
        # 要么都保留，要么都移除
        assert call_in_kept == result_in_kept


@pytest.mark.asyncio
class TestToolChainAwareStrategy:
    async def test_keeps_tool_chain_intact(self):
        strategy = ToolChainAwareStrategy(max_tokens=50)
        messages = [
            {"role": "user", "content": "question"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "function": {"name": "tool_a"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {"role": "assistant", "content": "final"},
        ]
        result = await strategy.compress(messages, CompressionContext())
        # All messages fit, nothing pruned
        assert not result.pruned_messages

    async def test_removes_entire_tool_chain_together(self):
        strategy = ToolChainAwareStrategy(max_tokens=1)
        messages = [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "function": {"name": "tool_a"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "r"},
            {"role": "assistant", "content": "a"},
        ]
        result = await strategy.compress(messages, CompressionContext())
        # Must remove entire chain, never split
        pruned_ids = [m.get("tool_call_id") for m in result.pruned_messages]
        call_ids = [
            tc.get("id")
            for m in result.pruned_messages
            for tc in m.get("tool_calls", [])
        ]
        # If tool result is pruned, its call must also be pruned
        if "call_1" in pruned_ids:
            assert "call_1" in call_ids

    async def test_does_not_split_tool_call_and_result(self):
        strategy = ToolChainAwareStrategy(max_tokens=1)
        messages = [
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "function": {"name": "calc"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "42"},
        ]
        result = await strategy.compress(messages, CompressionContext())
        kept = [m for m in messages if m not in result.pruned_messages]
        call_in_kept = any(m.get("role") == "assistant" and m.get("tool_calls") for m in kept)
        result_in_kept = any(m.get("role") == "tool" for m in kept)
        # 要么都保留，要么都移除
        assert call_in_kept == result_in_kept


@pytest.mark.asyncio
class TestHybridCompressionStrategy:
    async def test_applies_strategies_in_sequence(self):
        truncation = TruncationStrategy(target_count=5)
        token_window = TokenWindowStrategy(max_tokens=1)
        hybrid = HybridCompressionStrategy([truncation, token_window])

        messages = [{"role": "user", "content": str(i) * 50} for i in range(10)]
        result = await hybrid.compress(messages, CompressionContext())

        # 先截断到 5 条，再由 token_window 进一步压缩
        assert len(result.pruned_messages) >= 5
        # 至少保留了最新的一条
        assert messages[-1] not in result.pruned_messages

    async def test_hybrid_does_not_split_tool_chain(self):
        truncation = TruncationStrategy(target_count=2)
        tool_chain = ToolChainAwareStrategy(max_tokens=1)
        hybrid = HybridCompressionStrategy([truncation, tool_chain])

        messages = [
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "function": {"name": "calc"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "42"},
        ]
        result = await hybrid.compress(messages, CompressionContext())
        kept = result.remaining_messages
        call_in_kept = any(
            m.get("role") == "assistant" and m.get("tool_calls") for m in kept
        )
        result_in_kept = any(m.get("role") == "tool" for m in kept)
        assert call_in_kept == result_in_kept
