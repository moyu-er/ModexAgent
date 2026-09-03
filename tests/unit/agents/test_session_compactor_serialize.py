"""Golden tests for SessionCompactorAgent._serialize_messages.

Locks the plain-text transcript format fed to the compaction LLM, with focus
on the ``[Assistant reasoning]`` line: presence, ordering (before tool calls
and content), truncation budget (tool_output_max_chars), and byte-identical
output for messages without reasoning.
"""

from __future__ import annotations

from typing import Any

from modex_agent.agents.summarizer.session_compactor import (
    SessionCompactorAgent,
    SessionCompactorConfig,
)
from modex_agent.core.llm_struct import LLMResponse
from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.core.provider import CallbackStreamProvider


class _UnusedProvider(CallbackStreamProvider):
    """The serializer is pure — the provider is never called."""

    def __init__(self) -> None:
        super().__init__()

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs: Any,
    ) -> LLMResponse:
        del messages, model, temperature, max_output_tokens, tools, kwargs
        raise AssertionError("chat_stream must not be called by _serialize_messages")

    def get_default_model(self) -> str:
        return "unused-model"


def _agent(tool_output_max_chars: int = 2000) -> SessionCompactorAgent:
    return SessionCompactorAgent(
        _UnusedProvider(),
        SessionCompactorConfig(tool_output_max_chars=tool_output_max_chars),
    )


class TestReasoningSerialization:
    def test_reasoning_precedes_tool_calls_and_content(self) -> None:
        messages: list[dict[str, Any]] = [
            {
                "role": str(MessageRole.ASSISTANT),
                "content": "我来排查这个问题",
                "tool_calls": [
                    {
                        "id": "call_b1",
                        "type": "function",
                        "function": {"name": "read", "arguments": '{"path": "/a.py"}'},
                    },
                ],
                "reasoning_content": "先读文件确认问题所在",
            },
        ]
        result = _agent()._serialize_messages(messages)
        lines = result.splitlines()
        assert lines == [
            "[Assistant reasoning]: 先读文件确认问题所在",
            "[Assistant tool calls]: read({\"path\": \"/a.py\"})",
            "[Assistant]: 我来排查这个问题",
        ]

    def test_reasoning_without_tool_calls_or_content(self) -> None:
        messages: list[dict[str, Any]] = [
            {
                "role": str(MessageRole.ASSISTANT),
                "content": None,
                "reasoning_content": "只有思考,没有正文",
            },
        ]
        assert _agent()._serialize_messages(messages) == "[Assistant reasoning]: 只有思考,没有正文"

    def test_overlong_reasoning_truncated_to_tool_output_budget(self) -> None:
        reasoning = "x" * 50
        messages: list[dict[str, Any]] = [
            {"role": str(MessageRole.ASSISTANT), "content": None, "reasoning_content": reasoning},
        ]
        result = _agent(tool_output_max_chars=20)._serialize_messages(messages)
        assert result == f"[Assistant reasoning]: {'x' * 20}\n... (50 chars total)"

    def test_reasoning_exactly_at_budget_not_truncated(self) -> None:
        reasoning = "y" * 20
        messages: list[dict[str, Any]] = [
            {"role": str(MessageRole.ASSISTANT), "content": None, "reasoning_content": reasoning},
        ]
        assert _agent(tool_output_max_chars=20)._serialize_messages(messages) == (
            f"[Assistant reasoning]: {reasoning}"
        )

    def test_blank_reasoning_renders_nothing(self) -> None:
        messages: list[dict[str, Any]] = [
            {
                "role": str(MessageRole.ASSISTANT),
                "content": "正文",
                "reasoning_content": "   ",
            },
        ]
        assert _agent()._serialize_messages(messages) == "[Assistant]: 正文"

    def test_reasoning_on_non_assistant_role_not_rendered(self) -> None:
        messages: list[dict[str, Any]] = [
            {
                "role": str(MessageRole.USER),
                "content": "用户消息",
                "reasoning_content": "不该出现",
            },
        ]
        assert _agent()._serialize_messages(messages) == "[User]: 用户消息"

    def test_compact_role_with_reasoning_still_skipped(self) -> None:
        messages: list[dict[str, Any]] = [
            {
                "role": str(MessageRole.COMPACT),
                "content": "## Objective\n旧摘要",
                "reasoning_content": "COMPACT 轮的 reasoning 也不序列化",
            },
        ]
        assert _agent()._serialize_messages(messages) == ""


class TestNoReasoningUnchanged:
    """Messages without reasoning must serialize exactly as before."""

    def test_assistant_with_tool_calls_only(self) -> None:
        messages: list[dict[str, Any]] = [
            {
                "role": str(MessageRole.ASSISTANT),
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_c1",
                        "type": "function",
                        "function": {"name": "grep", "arguments": '{"pattern": "x"}'},
                    },
                ],
            },
        ]
        assert _agent()._serialize_messages(messages) == (
            "[Assistant tool calls]: grep({\"pattern\": \"x\"})"
        )

    def test_user_and_tool_messages(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": str(MessageRole.USER), "content": "查一下"},
            {
                "role": str(MessageRole.TOOL),
                "content": "结果" * 3000,
            },
        ]
        result = _agent(tool_output_max_chars=100)._serialize_messages(messages)
        lines = result.splitlines()
        assert lines[0] == "[User]: 查一下"
        assert lines[1].startswith("[Tool result]: " + "结果" * 50)
        assert lines[-1] == "... (6000 chars total)"
