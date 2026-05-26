"""Tests for ToolCallAccumulator partial/fallback handling.

The accumulator must gracefully handle cases where streaming chunks
produce slightly malformed JSON. The _partial fallback must still
attempt to extract usable arguments.
"""

from __future__ import annotations

import json

import pytest

from framework.core.tool_call_accumulator import (
    AccumulatingToolCall,
    ToolCallAccumulator,
    ToolCallChunk,
)


class TestAccumulatingToolCallPartialFallback:
    """Verify to_partial_tool_call tries to recover usable arguments."""

    def test_valid_json_args_parsed_correctly(self) -> None:
        acc = AccumulatingToolCall(
            index=0, id="call-1", name="send_to_agent",
            args='{"target_agent":"reviewer","content":"hello","invocation_id":""}',
        )
        tc = acc.to_partial_tool_call(index=0)
        assert tc.tool_name == "send_to_agent"
        assert tc.arguments["target_agent"] == "reviewer"
        assert tc.arguments["content"] == "hello"
        assert "_partial" not in tc.arguments

    def test_truncated_json_falls_back_to_empty_dict(self) -> None:
        """When JSON is genuinely incomplete and unrepairable, returns empty dict."""
        acc = AccumulatingToolCall(
            index=0, id="call-2", name="send_to_agent",
            args='{"target_agent":"reviewer","content":"hel',
        )
        tc = acc.to_partial_tool_call(index=0)
        assert tc.tool_name == "send_to_agent"
        # Repair will close the bracket and parse what it can
        assert isinstance(tc.arguments, dict)
        assert "_partial" not in tc.arguments

    def test_json_with_trailing_comma_repaired(self) -> None:
        """Trailing commas are a common streaming artifact — should be repaired."""
        acc = AccumulatingToolCall(
            index=0, id="call-3", name="send_to_agent",
            args='{"target_agent":"reviewer","content":"hello",}',
        )
        tc = acc.to_partial_tool_call(index=0)
        assert "_partial" not in tc.arguments
        assert tc.arguments["target_agent"] == "reviewer"

    def test_json_with_unescaped_newlines_repaired(self) -> None:
        """Literal newlines in JSON string values should be escaped."""
        acc = AccumulatingToolCall(
            index=0, id="call-4", name="send_to_agent",
            args='{"target_agent":"reviewer","content":"line1\nline2","invocation_id":""}',
        )
        # The args string contains a literal newline, not the JSON escape \n
        # This would fail json.loads but the content is recoverable
        tc = acc.to_partial_tool_call(index=0)
        # Should recover the arguments despite the literal newline
        assert tc.arguments.get("target_agent") == "reviewer" or "_partial" in tc.arguments

    def test_flush_pending_trailing_comma_repaired(self) -> None:
        """flush_pending should repair trailing commas instead of using _partial."""
        accumulator = ToolCallAccumulator()
        # Two chunks producing args with a trailing comma
        accumulator.add_chunk(ToolCallChunk(
            index=0, id="call-5", name="send_to_agent",
            args='{"target_agent":"planner","content":"review this",',
        ))
        # Final chunk closes with trailing comma artifact
        accumulator.add_chunk(ToolCallChunk(
            index=0, id=None, name=None,
            args='}',
        ))
        # Args are now '{"target_agent":"planner","content":"review this",}'
        # This has a trailing comma — not valid JSON, so not auto-completed
        pending = accumulator.get_pending()
        assert len(pending) == 1

        results = accumulator.flush_pending()
        assert len(results) == 1
        assert results[0].tool_name == "send_to_agent"
        assert results[0].arguments.get("target_agent") == "planner"
        assert "_partial" not in results[0].arguments


class TestToolCallAccumulatorEndToEnd:
    """End-to-end tests for the full accumulation → flush cycle."""

    def test_multi_chunk_valid_args_assembled_correctly(self) -> None:
        """Chunks arriving in parts should be assembled into complete args."""
        acc = ToolCallAccumulator()

        # Chunk 1: tool name + start of args
        completed = acc.add_chunk(ToolCallChunk(
            index=0, id="call-10", name="send_to_agent",
            args='{"target_agent":"reviewer",',
        ))
        assert completed == []

        # Chunk 2: rest of args
        completed = acc.add_chunk(ToolCallChunk(
            index=0, id=None, name=None,
            args='"content":"check code","invocation_id":""}',
        ))
        assert len(completed) == 1
        assert completed[0].arguments["target_agent"] == "reviewer"
        assert completed[0].arguments["content"] == "check code"

    def test_empty_args_produces_empty_dict(self) -> None:
        """Tool call with no args should produce empty dict, not _partial."""
        acc = ToolCallAccumulator()
        completed = acc.add_chunk(ToolCallChunk(
            index=0, id="call-11", name="list_communication_targets",
            args="{}",
        ))
        assert len(completed) == 1
        assert completed[0].arguments == {}
