"""Agent message normalization and system-reminder wrapping utilities.

Normalizes internal non-standard message roles to LLM-compatible format:
- ``compact`` -> ``assistant`` (role replacement, content unchanged)
- ``system_reminder`` -> ``user`` (role replacement, content already wrapped
  at storage time via :func:`wrap_system_reminder`)
- ``agent`` -> ``user`` (role replacement, content unchanged)

:func:`wrap_system_reminder` wraps markdown content in a
``<system-reminder>`` XML envelope (no attributes) for use by message
builders and hooks at storage time.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from modex_agent.core.message import ChatMessage, MessageRole


def _msg_to_dict(msg: ChatMessage | dict[str, Any]) -> dict[str, Any]:
    """将 ChatMessage 或 dict 统一转换为 dict。"""
    if isinstance(msg, ChatMessage):
        return msg.to_dict()
    return msg


def wrap_system_reminder(content: str) -> str:
    """Wrap free-form reminder content in a ``<system-reminder>`` envelope.

    The reminder tag carries no XML attributes — callers that need to attach
    provenance metadata (source agent, invocation id, etc.) store it on the
    ``ChatMessage`` extra fields, not on the tag itself. The content is
    whitespace-trimmed so stored reminders stay compact regardless of how the
    upstream caller built the string.

    Args:
        content: Raw reminder text to wrap.

    Returns:
        ``<system-reminder>\\n{content.strip()}\\n</system-reminder>``
    """
    return f"<system-reminder>\n{content.strip()}\n</system-reminder>"


def sanitize_reminder_content(content: str) -> str:
    """Strip nested system envelopes before reminder storage or delivery."""
    if not content:
        return content
    sanitized = re.sub(
        r"<\s*system(?:-reminder)?\b[^>]*>.*?<\s*/\s*system(?:-reminder)?\s*>",
        "",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return re.sub(r"\n{3,}", "\n\n", sanitized).strip()


def normalize_agent_messages_for_llm(
    messages: Sequence[ChatMessage | dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert internal non-standard role messages to an LLM-compatible form.

    Conversion rules:
    - ``role: "compact"`` → ``role: "assistant"`` (pure role replacement,
      content untouched)
    - ``role: "system_reminder"`` → ``role: "user"`` (pure role replacement,
      content untouched — the ``<system-reminder>`` envelope is already applied
      at storage time via ``wrap_system_reminder``)
    - ``role: "agent"`` → ``role: "user"`` (pure role replacement,
      content untouched)
    - Other role messages are passed through unchanged

    Args:
        messages: Raw message list (may contain non-standard roles),
            ``ChatMessage`` or ``dict``.

    Returns:
        Converted message list (a new list; the original data is not mutated).
    """
    converted: list[dict[str, Any]] = []

    for msg in messages:
        msg_dict = _msg_to_dict(msg)
        role = msg_dict.get("role")

        # COMPACT → ASSISTANT (pure role replacement, no content change)
        if role == MessageRole.COMPACT:
            converted.append({**msg_dict, "role": MessageRole.ASSISTANT})
            continue

        # SYSTEM_REMINDER → USER (pure role replacement; content already wrapped)
        if role == MessageRole.SYSTEM_REMINDER:
            converted.append({**msg_dict, "role": MessageRole.USER})
            continue

        # AGENT → USER (pure role replacement; content untouched)
        if role == MessageRole.AGENT:
            converted.append({**msg_dict, "role": MessageRole.USER})
            continue

        # Other roles pass through unchanged
        converted.append(msg_dict)

    return converted
