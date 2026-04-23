"""Tests for SemanticMessageFilter."""

import pytest

from framework.memory.compression.semantic_filter import SemanticMessageFilter
from framework.memory.compression.strategy import MessageSemanticValue


@pytest.fixture
def filt():
    return SemanticMessageFilter()


class TestClassify:
    def test_user_is_high(self, filt):
        assert filt.classify({"role": "user", "content": "hi"}) == MessageSemanticValue.HIGH

    def test_system_is_high(self, filt):
        assert filt.classify({"role": "system", "content": "prompt"}) == MessageSemanticValue.HIGH

    def test_assistant_without_tool_calls_is_high(self, filt):
        assert filt.classify({"role": "assistant", "content": "hello"}) == MessageSemanticValue.HIGH

    def test_assistant_with_tool_calls_is_derived(self, filt):
        assert (
            filt.classify({"role": "assistant", "content": "", "tool_calls": [{"id": "tc1"}]})
            == MessageSemanticValue.DERIVED
        )

    def test_tool_low_by_default(self, filt):
        assert filt.classify({"role": "tool", "content": "big json", "name": "shell"}) == MessageSemanticValue.LOW

    def test_tool_medium_when_whitelisted(self, filt):
        assert (
            filt.classify({"role": "tool", "content": "result", "name": "web_search"})
            == MessageSemanticValue.MEDIUM
        )


class TestSanitize:
    def test_keeps_high_and_medium(self, filt):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1", "function": {"name": "web_search"}}]},
            {"role": "tool", "content": "result", "tool_call_id": "tc1", "name": "web_search"},
        ]
        result = filt.sanitize(msgs)
        assert len(result) == 3

    def test_drops_low_value_tool_results(self, filt):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1", "function": {"name": "shell"}}]},
            {"role": "tool", "content": "lots of data", "tool_call_id": "tc1", "name": "shell"},
            {"role": "user", "content": "bye"},
        ]
        result = filt.sanitize(msgs)
        # tool result is LOW, so the chain becomes orphan and is folded
        assert len(result) == 3
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert "Called tools" in result[1]["content"]
        assert result[2]["role"] == "user"

    def test_complete_orphan_chain_at_end_of_list(self, filt):
        """spec:message-filter-strategy 'Complete orphan chain at end of list'"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1", "function": {"name": "shell"}}]},
            {"role": "tool", "content": "data", "tool_call_id": "tc1", "name": "shell"},
        ]
        result = filt.sanitize(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert "Called tools" in result[0]["content"]

    def test_consecutive_orphan_chains(self, filt):
        """spec:message-filter-strategy 'Consecutive orphan chains'"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1", "function": {"name": "shell"}}]},
            {"role": "tool", "content": "d1", "tool_call_id": "tc1", "name": "shell"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc2", "function": {"name": "list_dir"}}]},
            {"role": "tool", "content": "d2", "tool_call_id": "tc2", "name": "list_dir"},
        ]
        result = filt.sanitize(msgs)
        assert len(result) == 2
        assert all(m["role"] == "assistant" for m in result)
        assert "shell" in result[0]["content"]
        assert "list_dir" in result[1]["content"]

    def test_empty_assistant_content_in_orphan_chain_is_discarded(self, filt):
        """spec:message-filter-strategy 'Empty assistant content in orphan chain'"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1", "function": {"name": "shell"}}]},
            {"role": "tool", "content": "data", "tool_call_id": "tc1", "name": "shell"},
        ]
        result = filt.sanitize(msgs)
        assert len(result) == 1
        assert result[0]["content"].strip() != ""

    def test_stray_tool_message_is_rejected(self, filt):
        """spec:message-filter-strategy 'Stray tool message in input'"""
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": "orphan", "tool_call_id": "tc1", "name": "shell"},
        ]
        result = filt.sanitize(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "user"

    def test_medium_tool_result_is_preserved(self, filt):
        """spec:message-filter-strategy 'MEDIUM tool result in input'"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1", "function": {"name": "web_search"}}]},
            {"role": "tool", "content": "search result", "tool_call_id": "tc1", "name": "web_search"},
        ]
        result = filt.sanitize(msgs)
        assert len(result) == 2
        assert result[0]["role"] == "assistant"
        assert result[1]["role"] == "tool"
        assert result[1]["content"] == "search result"
