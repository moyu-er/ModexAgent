"""Tests for prompt capture strategies (G2)."""

from __future__ import annotations

from typing import Any

import pytest

from modex_agent.core.message import ChatMessage
from modex_agent.core.types import MessageRole
from modex_agent.ioc.configs.observability import PromptCaptureMode
from modex_agent.trace.prompt_capture import (
    FullPromptCapture,
    HashPromptCapture,
    OffPromptCapture,
    SummaryPromptCapture,
    build_prompt_capture,
)
from modex_agent.trace.semconv import GenAiAttr

_SYSTEM_PROMPT = "You are a helpful assistant."


def _make_messages() -> list[ChatMessage]:
    return [
        ChatMessage(role=MessageRole.SYSTEM, content=_SYSTEM_PROMPT),
        ChatMessage(role=MessageRole.USER, content="Hello"),
        ChatMessage(role=MessageRole.ASSISTANT, content="Hi there!"),
        ChatMessage(role=MessageRole.USER, content="What is 2+2?"),
    ]


def _make_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "calculator",
                "description": "A simple calculator",
                "parameters": {
                    "type": "object",
                    "properties": {"expr": {"type": "string"}},
                    "required": ["expr"],
                },
            },
        }
    ]


# ── OffPromptCapture ──────────────────────────────────────────────────


def test_off_prompt_capture_returns_empty() -> None:
    strategy = OffPromptCapture()
    result = strategy.capture(_make_messages(), model=None)
    assert result == {}


def test_off_prompt_capture_with_model() -> None:
    strategy = OffPromptCapture()
    result = strategy.capture(_make_messages(), model="gpt-4")
    assert result == {GenAiAttr.REQUEST_MODEL: "gpt-4"}


# ── HashPromptCapture ─────────────────────────────────────────────────


def test_hash_prompt_capture_stores_hash() -> None:
    strategy = HashPromptCapture()
    result = strategy.capture(
        _make_messages(), model="gpt-4", system_prompt=_SYSTEM_PROMPT
    )
    assert GenAiAttr.SYSTEM_PROMPT_HASH in result
    assert GenAiAttr.SYSTEM_PROMPT_LENGTH in result
    assert result[GenAiAttr.SYSTEM_PROMPT_LENGTH] == len(_SYSTEM_PROMPT)
    assert GenAiAttr.INPUT_MESSAGES not in result
    assert GenAiAttr.REQUEST_TOOLS not in result


def test_hash_prompt_capture_falls_back_to_messages() -> None:
    strategy = HashPromptCapture()
    result = strategy.capture(_make_messages(), model=None)
    assert GenAiAttr.SYSTEM_PROMPT_HASH in result
    assert result[GenAiAttr.SYSTEM_PROMPT_LENGTH] == len(_SYSTEM_PROMPT)


# ── FullPromptCapture ─────────────────────────────────────────────────


def test_full_prompt_capture_includes_system_prompt() -> None:
    strategy = FullPromptCapture()
    result = strategy.capture(
        _make_messages(), model="gpt-4", system_prompt=_SYSTEM_PROMPT
    )
    assert GenAiAttr.SYSTEM_INSTRUCTIONS in result
    assert result[GenAiAttr.SYSTEM_INSTRUCTIONS] == _SYSTEM_PROMPT


def test_full_prompt_capture_includes_tool_definitions() -> None:
    strategy = FullPromptCapture()
    tools = _make_tools()
    result = strategy.capture(_make_messages(), model="gpt-4", tools=tools)
    assert GenAiAttr.REQUEST_TOOLS in result
    assert result[GenAiAttr.REQUEST_TOOLS] == tools


def test_full_prompt_capture_includes_full_messages() -> None:
    strategy = FullPromptCapture()
    msgs = _make_messages()
    long_text = "Message body " * 500
    for i in range(10):
        msgs.append(ChatMessage(role=MessageRole.USER, content=f"Message {i}: {long_text}"))
    result = strategy.capture(msgs, model=None)
    captured = result[GenAiAttr.INPUT_MESSAGES]
    assert isinstance(captured, list)
    assert len(captured) == len(msgs)
    last_parts = captured[-1]["parts"]
    assert isinstance(last_parts, list)
    text_content = last_parts[0]["content"]
    assert isinstance(text_content, str)
    assert "[...truncated" not in text_content


# ── SummaryPromptCapture backward compat ──────────────────────────────


def test_summary_still_works_with_new_kwargs() -> None:
    strategy = SummaryPromptCapture()
    result = strategy.capture(
        _make_messages(),
        model="gpt-4",
        tools=_make_tools(),
        system_prompt=_SYSTEM_PROMPT,
    )
    assert GenAiAttr.INPUT_MESSAGES in result
    assert GenAiAttr.REQUEST_MODEL in result
    assert GenAiAttr.SYSTEM_PROMPT_HASH in result
    assert GenAiAttr.SYSTEM_PROMPT_LENGTH in result


# ── build_prompt_capture routing ──────────────────────────────────────


def test_build_prompt_capture_routes_correctly() -> None:
    assert isinstance(build_prompt_capture(PromptCaptureMode.OFF), OffPromptCapture)
    assert isinstance(build_prompt_capture(PromptCaptureMode.HASH), HashPromptCapture)
    assert isinstance(build_prompt_capture(PromptCaptureMode.SUMMARY), SummaryPromptCapture)
    assert isinstance(build_prompt_capture(PromptCaptureMode.FULL), FullPromptCapture)
    assert isinstance(build_prompt_capture("off"), OffPromptCapture)
    assert isinstance(build_prompt_capture("hash"), HashPromptCapture)
    assert isinstance(build_prompt_capture("summary"), SummaryPromptCapture)
    assert isinstance(build_prompt_capture("full"), FullPromptCapture)


def test_build_prompt_capture_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown prompt_capture"):
        build_prompt_capture("nonexistent")
