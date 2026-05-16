"""Tests for framework.providers.shared.delta."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from framework.providers.shared.delta import (
    ParsedResponse,
    StreamDelta,
    extract_reasoning,
)


class TestExtractReasoning:
    """Unit tests for extract_reasoning()."""

    def test_returns_none_when_no_model_extra(self):
        model = MagicMock(spec=BaseModel)
        model.model_extra = None
        assert extract_reasoning(model) is None

    def test_returns_none_when_key_missing(self):
        model = MagicMock(spec=BaseModel)
        model.model_extra = {"other_field": "value"}
        assert extract_reasoning(model) is None

    def test_returns_reasoning_content(self):
        model = MagicMock(spec=BaseModel)
        model.model_extra = {"reasoning_content": "thinking..."}
        assert extract_reasoning(model) == "thinking..."

    def test_returns_none_for_none_input(self):
        assert extract_reasoning(None) is None


class TestStreamDeltaFromOpenai:
    """Unit tests for StreamDelta.from_openai()."""

    def test_extracts_content(self):
        delta = MagicMock()
        delta.content = "hello"
        delta.tool_calls = None
        delta.model_extra = None

        result = StreamDelta.from_openai(delta)
        assert result.content == "hello"
        assert result.reasoning_content is None
        assert result.tool_call_chunks == []
        assert result.finish_reason is None

    def test_extracts_reasoning_via_model_extra(self):
        delta = MagicMock()
        delta.content = None
        delta.tool_calls = None
        delta.model_extra = {"reasoning_content": "let me think..."}

        result = StreamDelta.from_openai(delta)
        assert result.reasoning_content == "let me think..."

    def test_extracts_tool_call_chunks(self):
        func_chunk = MagicMock()
        func_chunk.name = "search"
        func_chunk.arguments = '{"query": "weather"}'

        tc = MagicMock()
        tc.index = 0
        tc.id = "call_001"
        tc.function = func_chunk

        delta = MagicMock()
        delta.content = None
        delta.tool_calls = [tc]
        delta.model_extra = None

        result = StreamDelta.from_openai(delta)
        assert len(result.tool_call_chunks) == 1
        chunk = result.tool_call_chunks[0]
        assert chunk.index == 0
        assert chunk.id == "call_001"
        assert chunk.name == "search"
        assert chunk.args == '{"query": "weather"}'

    def test_handles_none_function(self):
        tc = MagicMock()
        tc.index = 0
        tc.id = "call_002"
        tc.function = None

        delta = MagicMock()
        delta.content = None
        delta.tool_calls = [tc]
        delta.model_extra = None

        result = StreamDelta.from_openai(delta)
        assert result.tool_call_chunks[0].name is None
        assert result.tool_call_chunks[0].args is None

    def test_default_values(self):
        d = StreamDelta()
        assert d.content is None
        assert d.reasoning_content is None
        assert d.tool_call_chunks == []


class TestParsedResponseFromOpenai:
    """Unit tests for ParsedResponse.from_openai()."""

    def test_extracts_simple_content_response(self):
        msg = MagicMock()
        msg.content = "Hello, world!"
        msg.tool_calls = None
        msg.model_extra = None

        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "stop"

        usage = MagicMock()
        usage.prompt_tokens = 100
        usage.completion_tokens = 50
        usage.total_tokens = 150

        response = MagicMock()
        response.choices = [choice]
        response.usage = usage

        result = ParsedResponse.from_openai(response)
        assert result.content == "Hello, world!"
        assert result.finish_reason == "stop"
        assert result.usage == {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }
        assert result.tool_calls == []

    def test_extracts_tool_calls(self):
        func = MagicMock()
        func.name = "search"
        func.arguments = '{"query": "weather"}'

        tc = MagicMock()
        tc.id = "call_003"
        tc.function = func

        msg = MagicMock()
        msg.content = None
        msg.tool_calls = [tc]
        msg.model_extra = None

        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "tool_calls"

        response = MagicMock()
        response.choices = [choice]
        response.usage = None

        result = ParsedResponse.from_openai(response)
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].tool_name == "search"
        assert result.tool_calls[0].arguments == {"query": "weather"}
        assert result.tool_calls[0].call_id == "call_003"
        assert result.finish_reason == "tool_calls"

    def test_extracts_reasoning_content(self):
        msg = MagicMock()
        msg.content = "answer"
        msg.tool_calls = None
        msg.model_extra = {"reasoning_content": "step by step..."}

        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "stop"

        response = MagicMock()
        response.choices = [choice]
        response.usage = None

        result = ParsedResponse.from_openai(response)
        assert result.reasoning_content == "step by step..."

    def test_handles_none_usage(self):
        msg = MagicMock()
        msg.content = "ok"
        msg.tool_calls = None
        msg.model_extra = None

        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "stop"

        response = MagicMock()
        response.choices = [choice]
        response.usage = None

        result = ParsedResponse.from_openai(response)
        assert result.usage == {}

    # ── malformed tool call argument handling ──

    def test_malformed_tool_args_single_quotes(self):
        """Tool call arguments with single quotes (Python-style) → degrade to {}."""
        func = MagicMock()
        func.name = "search"
        func.arguments = "{'key': 'value'}"

        tc = MagicMock()
        tc.id = "call_m1"
        tc.function = func

        msg = MagicMock()
        msg.content = None
        msg.tool_calls = [tc]
        msg.model_extra = None

        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "tool_calls"

        response = MagicMock()
        response.choices = [choice]
        response.usage = None

        result = ParsedResponse.from_openai(response)
        # Tool call is preserved, but with empty args instead of crashing
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].tool_name == "search"
        assert result.tool_calls[0].arguments == {}
        assert result.tool_calls[0].call_id == "call_m1"

    def test_malformed_tool_args_missing_quotes(self):
        """Tool call arguments with unquoted keys → degrade to {}."""
        func = MagicMock()
        func.name = "run_shell"
        func.arguments = "{cmd: ls}"

        tc = MagicMock()
        tc.id = "call_m2"
        tc.function = func

        msg = MagicMock()
        msg.content = None
        msg.tool_calls = [tc]
        msg.model_extra = None

        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "tool_calls"

        response = MagicMock()
        response.choices = [choice]
        response.usage = None

        result = ParsedResponse.from_openai(response)
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].tool_name == "run_shell"
        assert result.tool_calls[0].arguments == {}

    def test_malformed_tool_args_truncated_json(self):
        """Tool call arguments with truncated/incomplete JSON → degrade to {}."""
        func = MagicMock()
        func.name = "read_file"
        func.arguments = '{"path": "/tmp'

        tc = MagicMock()
        tc.id = "call_m3"
        tc.function = func

        msg = MagicMock()
        msg.content = None
        msg.tool_calls = [tc]
        msg.model_extra = None

        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "tool_calls"

        response = MagicMock()
        response.choices = [choice]
        response.usage = None

        result = ParsedResponse.from_openai(response)
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].tool_name == "read_file"
        assert result.tool_calls[0].arguments == {}

    def test_malformed_tool_args_among_valid_ones(self):
        """Only the malformed tool call is degraded; valid ones are unaffected."""
        valid_func = MagicMock()
        valid_func.name = "search"
        valid_func.arguments = '{"query": "ok"}'

        bad_func = MagicMock()
        bad_func.name = "run_shell"
        bad_func.arguments = "{invalid}"

        tc1 = MagicMock()
        tc1.id = "call_ok"
        tc1.function = valid_func

        tc2 = MagicMock()
        tc2.id = "call_bad"
        tc2.function = bad_func

        msg = MagicMock()
        msg.content = None
        msg.tool_calls = [tc1, tc2]
        msg.model_extra = None

        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "tool_calls"

        response = MagicMock()
        response.choices = [choice]
        response.usage = None

        result = ParsedResponse.from_openai(response)
        assert len(result.tool_calls) == 2
        assert result.tool_calls[0].arguments == {"query": "ok"}
        assert result.tool_calls[1].arguments == {}

    def test_malformed_tool_args_empty_string(self):
        """Empty arguments string → degrade to {}."""
        func = MagicMock()
        func.name = "list_dir"
        func.arguments = ""

        tc = MagicMock()
        tc.id = "call_m4"
        tc.function = func

        msg = MagicMock()
        msg.content = None
        msg.tool_calls = [tc]
        msg.model_extra = None

        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "tool_calls"

        response = MagicMock()
        response.choices = [choice]
        response.usage = None

        result = ParsedResponse.from_openai(response)
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].arguments == {}
