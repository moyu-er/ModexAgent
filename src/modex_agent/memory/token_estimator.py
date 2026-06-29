"""Token estimation seam.

A single ``TokenEstimator`` instance is injected into every site that counts
tokens (compression trigger, boundary walk, request-time governance) so they
all agree on what "over budget" means. The framework ships a zero-dependency
char-based default; the example bot supplies a tiktoken-backed estimator.

Both estimators count the SAME message fields (content, name, tool_call_id,
tool_calls JSON) plus a per-message overhead — only the text-to-token
encoding differs. ``reasoning_content`` is excluded (stripped before
persistence, so it never occupies the session).
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from modex_agent.core.message import ChatMessage


def message_payload(message: dict[str, Any]) -> str:
    """Concatenate every token-bearing field of a session message dict.

    Counts content (str / list-of-parts / other), name, tool_call_id, and
    tool_calls JSON. ``reasoning_content`` is never present here — it is
    stripped before persistence — so it is not counted.
    """
    content = message.get("content")
    name = message.get("name")
    tool_call_id = message.get("tool_call_id")
    tool_calls = message.get("tool_calls")

    parts: list[str] = []

    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                txt = part.get("text")
                if isinstance(txt, str) and txt:
                    parts.append(txt)
                else:
                    parts.append(json.dumps(part, ensure_ascii=False))
            else:
                parts.append(str(part))
    elif content is not None:
        parts.append(json.dumps(content, ensure_ascii=False))

    if isinstance(name, str) and name:
        parts.append(name)
    if isinstance(tool_call_id, str) and tool_call_id:
        parts.append(tool_call_id)
    if tool_calls:
        parts.append(json.dumps(tool_calls, ensure_ascii=False))

    return "\n".join(parts)


def _char_tokens(text: str) -> int:
    """ASCII: 1 token / 4 chars. Non-ASCII (CJK etc.): 1 token / char."""
    if text.isascii():
        return len(text) // 4
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii = len(text) - ascii_chars
    return ascii_chars // 4 + non_ascii


class TokenEstimator(ABC):
    """Swappable token estimator shared by all token-counting sites."""

    #: per-message structural overhead added on top of the payload tokens
    MESSAGE_OVERHEAD = 4

    @abstractmethod
    def estimate_text(self, text: str) -> int:
        """Token count of a plain string."""

    def estimate_message(self, message: ChatMessage | dict[str, Any]) -> int:
        """Token count contributed by one message (payload + overhead).

        Accepts either a typed ``ChatMessage`` or the dict form used throughout
        the memory subsystem; the ``ChatMessage`` is normalized to its persisted
        dict (``to_dict``) once so ``message_payload`` works on a single shape.
        """
        data = message.to_dict() if isinstance(message, ChatMessage) else message
        payload = message_payload(data)
        if not payload:
            return self.MESSAGE_OVERHEAD
        return self.estimate_text(payload) + self.MESSAGE_OVERHEAD

    def estimate_messages(
        self, messages: list[ChatMessage | dict[str, Any]]
    ) -> int:
        """Sum of per-message token counts."""
        return sum(self.estimate_message(m) for m in messages)


class CharTokenEstimator(TokenEstimator):
    """Zero-dependency char-based estimator (framework default)."""

    def estimate_text(self, text: str) -> int:
        return _char_tokens(text)
