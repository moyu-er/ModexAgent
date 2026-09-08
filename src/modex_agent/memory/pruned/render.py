"""Markdown transcript renderer for pruned conversation memory.

Pure synchronous rendering of ``ChatMessage.to_dict()``-shaped message dicts
into the pruned transcript markdown format: a three-line header, then one
numbered block per message (block header + body), every block closed by a
``---`` separator, so ``grep "^## \\["`` yields a line-numbered message
table of contents. Segments are joined by blank lines (``"\\n\\n"``).

COMPACT filtering is NOT done here — ``PrunedManager.write_pruned`` owns the
"pruned = original conversation memory" invariant at its entry point.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from modex_agent.core.message import (
    ChatMessage,
    ImageUrlPart,
    MessageRole,
    TextPart,
    ToolCall,
    render_content_part_ref,
)

__all__ = ["render_transcript"]


def render_transcript(
    entry_id: int,
    topic: str,
    messages: list[dict[str, Any]],
    start: datetime | None,
    end: datetime | None,
) -> str:
    """Render pruned message dicts as a markdown transcript.

    ``messages`` follow the ``ChatMessage.to_dict()`` shape (OpenAI wire
    ``tool_calls``, ``"YYYY-MM-DD HH:MM:SS"`` ``created_at`` strings). A
    message that fails ``ChatMessage.from_dict`` degrades to a ``[raw]``
    block instead of being dropped — the transcript never fails as a whole.
    """
    range_line = (
        f"- range: {start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}"
        if start is not None and end is not None
        else "- range: unknown"
    )
    segments: list[str] = [
        f"# Transcript #{entry_id} · {topic}\n{range_line}\n- messages: {len(messages)}",
        "---",
    ]
    width = max(3, len(str(len(messages))))
    for index, raw in enumerate(messages, start=1):
        seq = f"{index:0{width}d}"
        try:
            msg = ChatMessage.from_dict(raw)
        except Exception:
            # Degradation boundary: any parse failure renders the original
            # dict as a [raw] block — messages are never dropped.
            segments += [f"## [{seq}] raw", json.dumps(raw, ensure_ascii=False, default=str), "---"]
            continue
        segments += [_block_header(seq, msg), _body(msg), "---"]
    return "\n\n".join(segments)


def _block_header(seq: str, msg: ChatMessage) -> str:
    """Block header: seq, role, optional name and tool_call_id, timestamp."""
    header = f"## [{seq}] {str(msg.role)}"
    if msg.name:
        header += f" · {msg.name}"
    if msg.tool_call_id:
        header += f" · {msg.tool_call_id}"
    return f"{header} · {msg.created_at:%m-%d %H:%M}"


def _body(msg: ChatMessage) -> str:
    """Block body: optional ``[reasoning]`` block, content, then tool_call lines.

    The reasoning chain renders verbatim — pruned transcripts are the original
    conversation record, so unlike the compaction serializer nothing is
    truncated here.
    """
    sections: list[str] = []
    reasoning = _reasoning_text(msg)
    if reasoning is not None:
        sections.append(f"[reasoning]\n{reasoning}")
    tool_lines = "\n".join(_tool_call_line(tc) for tc in msg.tool_calls or [])
    content = msg.content
    if content is not None:
        sections.append(_content_text(content))
    if tool_lines:
        sections.append(tool_lines)
    if not sections:
        return "(empty)"
    return "\n\n".join(sections)


def _reasoning_text(msg: ChatMessage) -> str | None:
    """Return the assistant reasoning chain verbatim, or None.

    ``reasoning_content`` is a declared ChatMessage field (ADR-0046) read
    via plain attribute access. Only assistant messages carry it;
    blank values render nothing.
    """
    if str(msg.role) != str(MessageRole.ASSISTANT):
        return None
    reasoning = msg.reasoning_content
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    return None


def _content_text(content: str | list[TextPart | ImageUrlPart]) -> str:
    """Render str content verbatim, or content parts one per line."""
    if isinstance(content, str):
        return content
    return "\n".join(_part_line(part) for part in content)


def _part_line(part: TextPart | ImageUrlPart) -> str:
    """TextPart renders its text; ImageUrlPart renders a placeholder."""
    return render_content_part_ref(part)


def _tool_call_line(tc: ToolCall) -> str:
    """One tool_call line: tool name, call id, and arguments."""
    args = (
        tc.arguments
        if isinstance(tc.arguments, str)
        else json.dumps(tc.arguments, ensure_ascii=False)
    )
    return f"[tool_call {tc.tool_name} · {tc.call_id}] {args}"
