"""Shared snapshot helpers for context-fork and experience-review paths.

Both paths extract recent messages and feed them to a child/reviewer agent.
This module converges the truncation window, per-message content cap, and
message field extraction that were previously duplicated (and diverging)
between ``ContextForkBuilder._messages_to_xml`` and
``ExperienceReviewHook._capture_snapshot``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

DEFAULT_SNAPSHOT_MAX_MESSAGES: int = 100
DEFAULT_SNAPSHOT_MAX_CONTENT_LEN: int = 2000


def _extract_role_content(m: Any) -> tuple[str, str, str | None]:
    if isinstance(m, dict):
        name = m.get("name")
        return (
            str(m.get("role", "unknown")),
            str(m.get("content", "") or ""),
            name if isinstance(name, str) else None,
        )
    name = getattr(m, "name", None)
    return (
        str(getattr(m, "role", "unknown")),
        str(getattr(m, "content", "") or ""),
        name if isinstance(name, str) else None,
    )


def _truncate_content(content: str, max_len: int) -> str:
    if len(content) <= max_len:
        return content
    return content[:max_len] + " [truncated]"


def format_snapshot_text(
    messages: Sequence[Any],
    *,
    max_messages: int = DEFAULT_SNAPSHOT_MAX_MESSAGES,
    max_content_len: int = DEFAULT_SNAPSHOT_MAX_CONTENT_LEN,
) -> str:
    """Format recent messages as ``[role]: content`` text lines.

    Used by ExperienceReviewHook for user-message injection.
    """
    if not messages:
        return ""
    recent = messages[-max_messages:] if len(messages) > max_messages else messages
    lines: list[str] = []
    for m in recent:
        role, content, _name = _extract_role_content(m)
        preview = _truncate_content(content, max_content_len)
        if preview.strip():
            lines.append(f"[{role}]: {preview}")
    return "\n".join(lines)


def format_snapshot_xml(
    messages: Sequence[Any],
    parent_name: str,
    *,
    max_content_len: int = DEFAULT_SNAPSHOT_MAX_CONTENT_LEN,
) -> str:
    """Format messages as ``<forked_context>`` XML.

    Used by ContextForkBuilder for system-prompt injection. Window truncation
    is NOT applied here — the caller handles window truncation and optional
    lossy compaction before calling this.
    """
    lines = [
        f'<forked_context source="{parent_name}">',
        f"  <info>Inherited {len(messages)} messages from parent session.</info>",
    ]
    for i, m in enumerate(messages):
        role, content, name = _extract_role_content(m)
        preview = _truncate_content(content, max_content_len)
        name_attr = f' name="{name}"' if role == "tool" and name else ""
        lines.append(f'  <message index="{i}" role="{role}"{name_attr}>')
        lines.append(f"    <![CDATA[{preview}]]>")
        lines.append("  </message>")
    lines.append("</forked_context>")
    return "\n".join(lines)
