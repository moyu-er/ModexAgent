"""Message builder utilities — pure functions that construct ChatMessage structs.

These functions do NOT perform business logic such as thinking-chain sanitization.
Callers (e.g. ReActAgent) are responsible for sanitising content before/after
building if required.
"""

from __future__ import annotations

from typing import Any

from modex_agent.core.message import ChatMessage, ContentFormat, ContentPart
from modex_agent.core.tool_manager import ToolResult
from modex_agent.core.types import MessageRole, ToolCall
from modex_agent.utils.xml import xml_attr, xml_text


def build_assistant_message(
    content: str | None,
    tool_calls: list[ToolCall],
    reasoning_content: str | None = None,
    reasoning_signature: str | None = None,
    reasoning_item_id: str | None = None,
    reasoning_encrypted_content: str | None = None,
) -> ChatMessage:
    """Construct an assistant ChatMessage.

    When *tool_calls* is non-empty and *content* is empty, OpenAI-compatible
    APIs require ``content`` to be ``null`` rather than an empty string.

    The reasoning fields are declared ``ChatMessage`` fields (ADR-0046): they
    travel with the message (e.g. EndNode can read ``reasoning_content`` for
    AgentResult) and survive persistence via :meth:`ChatMessage.to_dict` —
    the provider layer replays them conditionally per protocol (compat:
    ``reasoning_content`` on assistant tool-call turns; anthropic: thinking +
    signature every turn; responses: item_reference / encrypted_content).

    Args:
        content:           Message text from the LLM.
        tool_calls:        Parsed tool-call requests.
        reasoning_content: Raw thinking / reasoning chain (optional).
        reasoning_signature: Anthropic thinking signature (optional).
        reasoning_item_id: Responses API reasoning item id (optional).
        reasoning_encrypted_content: Responses API encrypted reasoning (optional).

    Returns:
        A :class:`ChatMessage` instance.
    """
    message_content = None if not content and tool_calls else content or ""

    extra: dict[str, Any] = {}
    if reasoning_content is not None:
        extra["reasoning_content"] = reasoning_content
    if reasoning_signature is not None:
        extra["reasoning_signature"] = reasoning_signature
    if reasoning_item_id is not None:
        extra["reasoning_item_id"] = reasoning_item_id
    if reasoning_encrypted_content is not None:
        extra["reasoning_encrypted_content"] = reasoning_encrypted_content

    if tool_calls:
        extra["tool_calls"] = tool_calls

    return ChatMessage(role=MessageRole.ASSISTANT, content=message_content, **extra)


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
        role=MessageRole.ASSISTANT,
        content="\n".join(parts),
        content_format=ContentFormat.XML,
        truncatable_paths=["content"],
    )


def build_tool_message(result: ToolResult, call_id: str | None = None) -> ChatMessage:
    """Construct a tool ChatMessage.

    Tool content must not be empty — it is padded with a single space if
    necessary to satisfy provider validation.

    The message content carries ``result.content`` verbatim: TextParts hold
    the observation text, ImageUrlParts hold the persisted ``media://``
    references (resolved into data URLs at each LLM call by
    ``inject_multimodal``, never persisted as base64).

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
    content: str | list[ContentPart] = result.content
    if not content:
        # Parts-free results (e.g. error-only) degrade to the rendered text
        # — the error text must stay visible to the model — with the single-
        # space provider-validation floor.
        content = result.message_content() or " "

    # Build ChatMessage, only passing XML metadata when detected
    extra: dict[str, Any] = {}
    if "content_format" in msg_dict:
        extra["content_format"] = msg_dict["content_format"]
    if "truncatable_paths" in msg_dict:
        extra["truncatable_paths"] = msg_dict["truncatable_paths"]

    return ChatMessage(
        role=MessageRole.TOOL,
        tool_call_id=call_id or result.tool_name,
        name=result.tool_name,
        content=content,
        **extra,
    )
