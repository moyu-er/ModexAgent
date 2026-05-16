"""Message builder utilities — pure functions that construct ChatMessage structs.

These functions do NOT perform business logic such as thinking-chain sanitization.
Callers (e.g. ReActAgent) are responsible for sanitising content before/after
building if required.
"""

from __future__ import annotations

import json
from typing import Any

from framework.core.tool_manager import ToolResult
from framework.core.types import ToolCall
from framework.memory.core.message import ChatMessage

MAX_TOOL_RESULT_CHARS: int = 20000  # ~5000 tokens


def build_assistant_message(
    content: str | None,
    tool_calls: list[ToolCall],
    reasoning_content: str | None = None,
) -> ChatMessage:
    """Construct an assistant ChatMessage.

    When *tool_calls* is non-empty and *content* is empty, OpenAI-compatible
    APIs require ``content`` to be ``null`` rather than an empty string.

    *reasoning_content* is stored as a pydantic-extra field so that it travels
    with the message (e.g. EndNode can read it for AgentResult) while
    :meth:`ChatMessage.to_dict` automatically strips it before storage.

    Args:
        content:           Message text from the LLM.
        tool_calls:        Parsed tool-call requests.
        reasoning_content: Raw thinking / reasoning chain (optional).

    Returns:
        A :class:`ChatMessage` instance.
    """
    message_content = None if not content and tool_calls else content or ""

    extra: dict[str, Any] = {}
    if reasoning_content is not None:
        extra["reasoning_content"] = reasoning_content

    if tool_calls:
        extra["tool_calls"] = [
            {
                "id": tc.call_id or f"call_{i}",
                "type": "function",
                "function": {
                    "name": tc.tool_name,
                    "arguments": json.dumps(tc.arguments) if tc.arguments else "{}",
                },
            }
            for i, tc in enumerate(tool_calls)
        ]

    return ChatMessage(role="assistant", content=message_content, **extra)


def build_tool_message(result: ToolResult, call_id: str | None = None) -> ChatMessage:
    """Construct a tool ChatMessage.

    Tool content must not be empty — it is padded with a single space if
    necessary to satisfy provider validation.

    Oversized results (> :data:`MAX_TOOL_RESULT_CHARS`) are truncated with an
    ellipsis suffix so the conversation history stays bounded.

    Args:
        result:  Tool execution result.
        call_id: Optional explicit tool-call ID; falls back to ``result.tool_name``.

    Returns:
        A :class:`ChatMessage` instance.
    """
    if result.error:
        content = f"Error: {result.error}"
    elif result.result is not None:
        content = str(result.result)
    else:
        content = " "

    if not content.strip():
        content = " "

    if len(content) > MAX_TOOL_RESULT_CHARS:
        content = content[:MAX_TOOL_RESULT_CHARS] + (
            f"\n... (truncated, {len(content)} chars total)"
        )

    return ChatMessage(
        role="tool",
        tool_call_id=call_id or result.tool_name,
        name=result.tool_name,
        content=content,
    )
