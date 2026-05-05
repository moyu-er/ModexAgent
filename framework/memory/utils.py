"""Shared memory utilities."""

import contextlib
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from framework.memory.core.message import ChatMessage

EMPTY_MEMORY_SUMMARY_MARKERS = frozenset({
    "(no conversation content)",
    "(no summary)",
    "(nothing)",
    "(no semantic content)",
})


def safe_atomic_replace(tmp_path: Path, target_path: Path) -> None:
    """Replace target with tmp file, with fallback for Windows file-locking.

    On Unix, ``os.replace`` is atomic and reliable. On Windows, it can fail
    with ``PermissionError`` when the target is held open by another process
    (antivirus, file indexer, concurrent writer). Falls back to a direct write
    in that case.

    Args:
        tmp_path: Temporary file with the new content.
        target_path: Destination file to replace.
    """
    try:
        os.replace(str(tmp_path), str(target_path))
    except OSError:
        content = tmp_path.read_text(encoding="utf-8")
        target_path.write_text(content, encoding="utf-8")
        with contextlib.suppress(OSError):
            tmp_path.unlink()

_RUNTIME_PREFIX_RE = re.compile(
    r"^\[Runtime Context\]\s*\n.*?\n\n",
    re.MULTILINE | re.DOTALL,
)


def _msg_to_dict(msg: ChatMessage | dict[str, Any]) -> dict[str, Any]:
    """将 ChatMessage 或 dict 统一转为 dict。"""
    return msg.to_dict() if isinstance(msg, ChatMessage) else dict(msg)


def strip_runtime_prefixes(
    messages: Sequence[ChatMessage | dict[str, Any]],
) -> list[dict[str, Any]]:
    """剥离消息内容中的 [Runtime Context] 前缀。

    Args:
        messages: 原始消息列表（ChatMessage 或 dict）

    Returns:
        清理后的消息列表（新对象，不修改原列表）
    """
    cleaned = []
    for msg in messages:
        m = _msg_to_dict(msg)
        content = m.get("content") or ""
        if content:
            content = _RUNTIME_PREFIX_RE.sub("", content)
            m["content"] = content
        cleaned.append(m)
    return cleaned


def normalize_memory_summary(summary: str | None) -> str | None:
    """Return a trimmed meaningful summary, or None for empty placeholders."""
    if summary is None:
        return None
    normalized = summary.strip()
    if not normalized:
        return None
    if normalized.lower() in EMPTY_MEMORY_SUMMARY_MARKERS:
        return None
    return normalized


def estimate_text_tokens(text: str) -> int:
    """Estimate token count for a plain text string.

    - ASCII characters: 1 token / 4 chars
    - Non-ASCII (CJK, etc.): 1 token / char (conservative)
    """
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return ascii_chars // 4 + non_ascii_chars


def estimate_token_count(messages: Sequence[ChatMessage | dict[str, Any]]) -> int:
    """基于字符数快速估算消息列表的 token 数。

    不需要引入 tiktoken 依赖，但对中文场景做了针对性修正。
    """
    total = 0
    for msg in messages:
        if isinstance(msg, ChatMessage):
            raw_content = msg.content or ""
            tool_calls = msg.tool_calls
        else:
            raw_content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls")
        content = raw_content if isinstance(raw_content, str) else str(raw_content)
        total += estimate_text_tokens(content)
        if tool_calls:
            for tc in tool_calls:
                total += estimate_text_tokens(str(tc))
    return total + len(messages) * 2
