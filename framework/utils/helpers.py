"""通用工具函数"""

from dataclasses import dataclass
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Thinking-chain extraction
# ---------------------------------------------------------------------------


class ThinkExtractionResult(NamedTuple):
    """思维链提取结果。"""

    cleaned: str | None
    """清理后的文本。None 表示输入为空或不完整 think 标签。"""
    reasoning: str | None
    """提取的思维链内容。None 表示没有思维链。"""


@dataclass(frozen=True)
class ThinkFormat:
    """思维链格式定义 — 纯字符串一对一匹配，不做任何 XML/格式假设。

    ``open_literal``: 开标签字面量（如 ``<think>``）
    ``end_marker``:   闭标签字面量（如 ``</think>``）

    匹配均为前缀 + 大小写不敏感：
    ``text.lower().startswith(open_literal.lower())``
    """

    name: str
    open_literal: str
    end_marker: str


# 内置思维链格式 —— 按 open_literal 长度降序（避免 <think> 误匹配 <thinking>）
BUILTIN_THINK_FORMATS: tuple[ThinkFormat, ...] = (
    ThinkFormat(name="thinking", open_literal="<thinking>", end_marker="</thinking>"),
    ThinkFormat(name="think", open_literal="<think>", end_marker="</think>"),
    ThinkFormat(name="tibetan", open_literal="༺", end_marker="༽"),
)


def _extract_format_prefix(text: str, fmt: ThinkFormat) -> ThinkExtractionResult:
    """如果 *text* 以 *fmt.open_literal* 开头，提取到第一个 *fmt.end_marker* 为止的内容。

    大小写不敏感，纯字符串操作。
    """
    lower = text.lower()
    open_lower = fmt.open_literal.lower()

    if not lower.startswith(open_lower):
        return ThinkExtractionResult(text, None)

    start = len(fmt.open_literal)
    end_pos = lower.find(fmt.end_marker.lower(), start)
    if end_pos == -1:
        return ThinkExtractionResult(None, None)

    reasoning = text[start:end_pos]
    after = text[end_pos + len(fmt.end_marker):]
    # 去掉闭标签后紧跟的换行
    if after.startswith("\r\n"):
        after = after[2:]
    elif after.startswith("\n") or after.startswith("\r"):
        after = after[1:]
    return ThinkExtractionResult(after, reasoning)


def looks_like_prefix(
    text: str,
    formats: tuple[ThinkFormat, ...],
) -> bool:
    """检查 *text* 是否可能是任何格式开标签的不完整前缀。"""
    lower = text.lower()

    for fmt in formats:
        open_lower = fmt.open_literal.lower()

        if len(lower) < len(open_lower):
            # text 比开标签短 → 可能是开标签的前缀（如 <thin 是 <think> 的前缀）
            if open_lower.startswith(lower):
                return True
        elif lower.startswith(open_lower):
            # text 以完整开标签开头 → 检查闭标签是否存在
            start = len(fmt.open_literal)
            if lower.find(fmt.end_marker.lower(), start) == -1:
                return True

    return False


def extract_think_prefix(
    text: str | None,
    formats: tuple[ThinkFormat, ...] | None = None,
) -> ThinkExtractionResult:
    """Extract thinking-chain prefix from text — 纯字符串匹配。

    支持的格式由 *formats* 参数控制，默认使用内置格式。
    所有匹配均为纯字符串操作（大小写不敏感），不做任何格式假设。
    """
    if not text:
        return ThinkExtractionResult(None, None)

    cleaned = text.strip()
    fmts = formats if formats is not None else BUILTIN_THINK_FORMATS

    for fmt in fmts:
        result = _extract_format_prefix(cleaned, fmt)
        if result.reasoning is not None:
            return result
        if result.cleaned is None:
            return ThinkExtractionResult(None, None)

    if looks_like_prefix(cleaned, fmts):
        return ThinkExtractionResult(None, None)

    return ThinkExtractionResult(cleaned, None)


def strip_think(text: str | None) -> str | None:
    """去除前缀形式的思维链内容。

    这是 ``extract_think_prefix`` 的便捷包装，只返回清理后的文本。
    """
    result = extract_think_prefix(text)
    return result.cleaned if result.cleaned else None
