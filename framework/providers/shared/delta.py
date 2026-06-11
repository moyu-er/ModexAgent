"""Shared intermediate types for provider response parsing.

StreamDelta  — streaming chunk extraction result.
ParsedResponse — non-streaming response extraction result.
extract_reasoning — reasoning_content extraction from Pydantic model_extra.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from framework.core.tool_call_accumulator import ToolCallChunk
from framework.core.types import ToolCall

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pydantic import BaseModel


def extract_reasoning(model: BaseModel | None) -> str | None:
    """Extract reasoning_content from a Pydantic model's extra fields.

    reasoning_content is not in the openai SDK typed schema;
    it arrives via model_extra, Pydantic's official extension mechanism.
    No getattr/hasattr reflection is used.
    """
    if model is None:
        return None
    extra = model.model_extra
    if extra is None:
        return None
    return extra.get("reasoning_content")


@dataclass
class StreamDelta:
    """Structured extraction from a streaming chunk's delta.

    All fields are typed; no dicts, no hasattr probing.
    """

    content: str | None = None
    reasoning_content: str | None = None
    tool_call_chunks: list[ToolCallChunk] = field(default_factory=list)
    finish_reason: str | None = None

    @classmethod
    def from_openai(cls, delta) -> StreamDelta:
        """Build from openai SDK ChoiceDelta Pydantic object.

        Args:
            delta: openai.types.chat.chat_completion_chunk.ChoiceDelta

        All field access is via typed attribute access on the SDK model.
        reasoning_content uses extract_reasoning() via model_extra.
        """
        instance = cls()
        instance.content = delta.content

        if delta.tool_calls:
            instance.tool_call_chunks = [
                ToolCallChunk(
                    index=tc.index,
                    id=tc.id,
                    name=tc.function.name if tc.function else None,
                    args=tc.function.arguments if tc.function else None,
                )
                for tc in delta.tool_calls
            ]

        instance.reasoning_content = extract_reasoning(delta)
        return instance


@dataclass
class ParsedResponse:
    """Structured extraction from a non-streaming LLM response.

    Intermediate carrier between SDK response and framework LLMResponse.
    """

    content: str | None
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_openai(cls, response) -> ParsedResponse:
        """Build from openai SDK ChatCompletion Pydantic object.

        Args:
            response: openai.types.chat.ChatCompletion

        All field access is via typed attribute access on the SDK model.
        Tool call arguments are json.loads() parsed from JSON strings.
        """
        choice = response.choices[0]
        msg = choice.message

        tool_calls: list[ToolCall] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    logger.warning(
                        "Malformed tool call arguments for %s (call_id=%s): %.200s",
                        tc.function.name,
                        tc.id,
                        tc.function.arguments or "",
                    )
                    args = {}
                tool_calls.append(
                    ToolCall(
                        tool_name=tc.function.name,
                        arguments=args,
                        call_id=tc.id,
                    )
                )

        usage: dict[str, int] = {}
        if response.usage is not None:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        return cls(
            content=msg.content,
            reasoning_content=extract_reasoning(msg),
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )
