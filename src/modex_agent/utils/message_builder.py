"""Message builder utilities — pure functions that construct ChatMessage structs.

These functions do NOT perform business logic such as thinking-chain sanitization.
Callers (e.g. ReActAgent) are responsible for sanitising content before/after
building if required.
"""

from __future__ import annotations

import json
from typing import Any

from modex_agent.core.tool_manager import ToolResult
from modex_agent.core.types import ToolCall
from modex_agent.core.message import ChatMessage, ContentFormat
from modex_agent.utils.xml import xml_attr, xml_text


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


def build_interrupted_assistant_message(
    content: str,
    pending_tool_names: list[str],
    reason: str,
) -> ChatMessage:
    """Construct an assistant ChatMessage marking a partially-produced response.

    Used when an LLM stream is interrupted mid-flight (user /stop, pause,
    timeout, error). Only the already-produced *content* (no reasoning) is
    preserved; tool calls are discarded (no results to fill) but, if any tool
    names are known, they are noted as pending so the agent has context on
    resume.

    The body is XML and tagged ``content_format=XML`` with
    ``truncatable_paths=["content"]`` so the governance layer can truncate the
    (potentially long / unfinished) partial content without breaking structure.

    Args:
        content:            Partial assistant text accumulated before the interrupt.
        pending_tool_names: Names of tools the model intended to call (best-effort).
        reason:             Short, non-leaky interrupt category, e.g. ``user_stop``,
                            ``timeout``, ``error``. Becomes the ``reason`` attribute.

    Returns:
        A :class:`ChatMessage` with role ``assistant`` and XML body.
    """
    parts: list[str] = [f'<interrupted_response reason="{xml_attr(reason)}">']
    if content:
        parts.append(f"  <content>{xml_text(content)}</content>")
    if pending_tool_names:
        parts.append("  <pending_tools>")
        for name in pending_tool_names:
            parts.append(f'    <tool name="{xml_attr(name)}"/>')
        parts.append("  </pending_tools>")
    parts.append("</interrupted_response>")

    return ChatMessage(
        role="assistant",
        content="\n".join(parts),
        content_format=ContentFormat.XML,
        truncatable_paths=["content"],
    )


def build_tool_message(result: ToolResult, call_id: str | None = None) -> ChatMessage:
    """Construct a tool ChatMessage.

    Tool content must not be empty — it is padded with a single space if
    necessary to satisfy provider validation.

    Reuses ``ToolResult.to_message()`` for XML metadata detection
    (``content_format`` and ``truncatable_paths``), ensuring the governance
    layer can perform structure-aware compaction downstream.

    Truncation of oversized results is handled exclusively by the overflow
    interceptor — this function does NOT truncate.

    Args:
        result:  Tool execution result.
        call_id: Optional explicit tool-call ID; falls back to ``result.tool_name``.

    Returns:
        A :class:`ChatMessage` instance.
    """
    # Reuse to_message() which detects terminal XML and attaches
    # content_format / truncatable_paths metadata.
    msg_dict = result.to_message()
    content = msg_dict.get("content", " ")
    if not content or not content.strip():
        content = " "

    # Build ChatMessage, only passing XML metadata when detected
    extra: dict[str, Any] = {}
    if "content_format" in msg_dict:
        extra["content_format"] = msg_dict["content_format"]
    if "truncatable_paths" in msg_dict:
        extra["truncatable_paths"] = msg_dict["truncatable_paths"]

    return ChatMessage(
        role="tool",
        tool_call_id=call_id or result.tool_name,
        name=result.tool_name,
        content=content,
        **extra,
    )
