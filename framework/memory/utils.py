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
    "nothing to summarize",
    "no relevant content",
    "no meaningful information",
    "no content",
    "no information",
    "no data",
    "no context",
    "no conversation",
    "empty conversation",
    "the conversation is empty",
    "the conversation was brief",
    "this conversation",
    "n/a",
    "none",
    "empty",
    "brief exchange",
    "short conversation",
})


def _is_meaningless_summary(summary: str) -> bool:
    """Heuristic: detect obviously meaningless summaries beyond exact markers.

    Rejects summaries that are too short, too generic, or contain only
    meta-references without actual content.
    """
    text = summary.strip().lower()

    # Too short to be meaningful (< 5 chars is definitely not a summary)
    if len(text) < 5:
        return True

    # Mostly whitespace or punctuation
    if not re.search(r"[a-z一-鿿]", text):
        return True

    # Only generic meta-references, no concrete nouns/verbs
    generic_phrases = (
        "the user", "the assistant", "this conversation",
        "a conversation", "the conversation", "some messages",
    )
    # If the entire summary is just one generic phrase, reject it
    for phrase in generic_phrases:
        if text == phrase or text == phrase + ".":
            return True

    # Reject summaries that are raw tool-call XML (LLM hallucination — the
    # model echoed tool-call markup instead of producing a summary).
    _tool_xml_patterns = (
        "<minimax:tool_call>",
        "<tool_call>",
        "<function_call>",
        "<invoke name=",
    )
    if any(p in text for p in _tool_xml_patterns):
        return True

    return False


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

# Common preamble/postamble patterns that LLMs add to memory outputs
_MEMORY_PREAMBLE_PATTERNS = [
    # Chinese intros
    r"^(?:以下[是为]|下面[是为]).*?[：:]\s*\n?",
    r"^(?:让?我?来?看看|让?我?来?分析|让?我?来?总结).*?\n",
    r"^(?:好[的嗯]|没问题|明白|了解|好的呢|好滴)[，。！]?.*?\n",
    r"^(?:根据|基于)[^\n]*(?:[。！？]\s*\n?|[：:]\s*\n)",
    r"^(?:这是|以下就?是).*?(?:回答|结果|总结|分析|内容)[，。：:]?\s*\n?",
    # English intros
    r"^(?:Here|Below)\s+(?:is|are)\s+(?:the\s+)?(?:summary|analysis|extraction|result|updated\s+content|consolidated\s+version)[：:.\s]*\n?",
    r"^(?:I\s+)?(?:have\s+)?(?:analyzed|summarized|extracted|consolidated).*?[.：:]\s*\n?",
    r"^(?:Let\s+me\s+)?(?:analyze|summarize|extract|look\s+at|check)\s*(?:this)?[.：:]?\s*\n?",
    # Generic polite wrappers
    r"^(?:Sure|Certainly|Of\s+course|Okay|Ok)[,!.]?\s*\n?",
    r"^(?:好的|没问题|可以|行)[，。！]?\s*\n?",
]

_MEMORY_PREAMBLE_RE = re.compile(
    "|".join(f"(?:{p})" for p in _MEMORY_PREAMBLE_PATTERNS),
    re.IGNORECASE | re.MULTILINE,
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


def strip_memory_preamble(text: str) -> str:
    """Remove common LLM preamble/postamble phrases from memory outputs."""
    result = text.strip()
    if not result:
        return result
    # Iteratively strip leading preamble patterns until stable
    for _ in range(5):
        cleaned = _MEMORY_PREAMBLE_RE.sub("", result)
        cleaned = cleaned.strip()
        if cleaned == result:
            break
        result = cleaned
    return result


def normalize_memory_summary(summary: str | None) -> str | None:
    """Return a trimmed meaningful summary, or None for empty placeholders."""
    if summary is None:
        return None
    normalized = summary.strip()
    if not normalized:
        return None
    normalized = strip_memory_preamble(normalized)
    if not normalized:
        return None
    if normalized.lower() in EMPTY_MEMORY_SUMMARY_MARKERS:
        return None
    if _is_meaningless_summary(normalized):
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
