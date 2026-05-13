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
