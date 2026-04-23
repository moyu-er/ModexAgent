"""Shared memory utilities."""

import re
from collections.abc import Sequence
from typing import Any

_RUNTIME_PREFIX_RE = re.compile(
    r"^\[Runtime Context\]\s*\n.*?\n\n",
    re.MULTILINE | re.DOTALL,
)


def strip_runtime_prefixes(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """剥离消息内容中的 [Runtime Context] 前缀。

    Args:
        messages: 原始消息列表

    Returns:
        清理后的消息列表（新对象，不修改原列表）
    """
    cleaned = []
    for msg in messages:
        m = dict(msg)
        content = m.get("content") or ""
        if content:
            content = _RUNTIME_PREFIX_RE.sub("", content)
            m["content"] = content
        cleaned.append(m)
    return cleaned


def _estimate_text_tokens(text: str) -> int:
    """更精确的逐字符 token 估算。

    - ASCII 字符（英文、数字、标点）：按 1 token / 4 chars
    - 非 ASCII（主要是 CJK 中文等）：按 1 token / char（保守估算）
    """
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return ascii_chars // 4 + non_ascii_chars


def estimate_token_count(messages: Sequence[dict[str, Any]]) -> int:
    """基于字符数快速估算消息列表的 token 数。

    不需要引入 tiktoken 依赖，但对中文场景做了针对性修正。
    """
    total = 0
    for msg in messages:
        content = msg.get("content") or ""
        total += _estimate_text_tokens(content)
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                total += _estimate_text_tokens(str(tc))
    return total + len(messages) * 2
