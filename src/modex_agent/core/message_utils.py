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
from enum import StrEnum
from typing import Any

from modex_agent.core.message import ChatMessage
from modex_agent.core.types import MessageRole


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


class ToolNudgeVerdict(StrEnum):
    """Terminal outcomes of the backward in-turn tool-usage scan."""

    USED = "used"
    """One of the target tools was used in the current turn's recent history."""

    DUE = "due"
    """Enough assistant steps accumulated without usage — a nudge is warranted."""

    SHORT_TURN = "short_turn"
    """The turn boundary was reached with too few assistant steps to judge."""


def scan_tool_usage_in_turn(
    messages: Sequence[ChatMessage],
    tool_names: frozenset[str],
    min_assistant_steps: int = 3,
) -> ToolNudgeVerdict:
    """Classify in-turn usage of ``tool_names`` for behavior-nudge decisions.

    Scans backwards from the latest message and stops at the first terminal
    outcome:

    - ``USED`` — a TOOL-role message carrying one of ``tool_names`` appears
      before the boundary: the tools were used in the current turn's recent
      history.
    - ``DUE`` — ``min_assistant_steps`` assistant messages were counted
      without a hit: enough steps have accumulated without usage.
    - ``SHORT_TURN`` — a ``user``/``agent`` role message (the logical turn
      boundary) was reached first, or the history ran out, with fewer than
      ``min_assistant_steps`` assistant messages in the segment. The turn
      has not accumulated enough steps to judge — nudges must NOT fire on
      it (this is what prevents the fresh-turn injection bug: at iteration
      zero the segment contains zero assistant messages).

    Boundary semantics: ``user`` and ``agent`` roles terminate the scan —
    they mark "the user spoke". ``agent`` is the same boundary by design;
    the framework itself never writes ``agent``-role messages (it only
    normalizes them away for the LLM), so it can only come from external
    input. ``system_reminder`` is deliberately TRANSPARENT: continuation
    hooks (todo continuation, deliver retry) and the nudge hooks themselves
    inject system-reminder messages mid-turn, so treating them as a
    boundary would fragment the current logical turn and make co-resident
    nudge hooks interfere with each other's scans.

    Tool results always sit between the assistant message that issued the
    call and the next one, so the backward scan covers calls interleaved
    among the counted assistant messages.

    Args:
        messages: Full message history (latest last).
        tool_names: Tool names to look for.
        min_assistant_steps: Number of assistant messages without usage
            that warrant a nudge. Defaults to 3; callers do not override it.

    Returns:
        The terminal verdict — ``USED``, ``DUE``, or ``SHORT_TURN``.
    """
    seen_assistant = 0
    for msg in reversed(messages):
        if msg.role in (MessageRole.USER, MessageRole.AGENT):
            return ToolNudgeVerdict.SHORT_TURN
        if msg.role == MessageRole.TOOL and msg.name in tool_names:
            return ToolNudgeVerdict.USED
        if msg.role == MessageRole.ASSISTANT:
            seen_assistant += 1
            if seen_assistant >= min_assistant_steps:
                return ToolNudgeVerdict.DUE
    return ToolNudgeVerdict.SHORT_TURN


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
