"""通用工具函数"""

import re
from dataclasses import dataclass
from typing import NamedTuple, Pattern


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
    """思维链格式定义。

    每个实例定义一种思维链的起始-结束标识对。
    ``start_pattern`` 用于 ``re.match`` 检测文本是否以该格式开头；
    ``end_marker`` 用于 ``str.find`` 定位结束位置。
    """

    name: str
    start_pattern: Pattern[str]
    end_marker: str


# 内置思维链格式 —— 按匹配优先级排序
BUILTIN_THINK_FORMATS: tuple[ThinkFormat, ...] = (
    ThinkFormat(
        name="think",
        start_pattern=re.compile(r"<think\b[^>]*>", re.IGNORECASE),
        end_marker="</think>",
    ),
    ThinkFormat(
        name="thinking",
        start_pattern=re.compile(r"<thinking\b[^>]*>", re.IGNORECASE),
        end_marker="</thinking>",
    ),
    ThinkFormat(
        name="tibetan",
        start_pattern=re.compile(r"༺"),
        end_marker="༽",
    ),
)


def _extract_format_prefix(text: str, fmt: ThinkFormat) -> ThinkExtractionResult:
    """如果 *text* 以 *fmt.start_pattern* 开头，提取到第一个 *fmt.end_marker* 为止的内容。

    思维链是线性文本，不存在嵌套。只需找到开头后第一个对应的结束标记，
    不需要括号匹配/嵌套计数。

    Args:
        text: 待处理的文本
        fmt:  思维链格式定义

    Returns:
        ThinkExtractionResult:
        - (text, None): 不以该格式开头
        - (None, None): 以该格式开头但未找到结束标记
        - (cleaned, reasoning): 结束标记后的内容 和 标记内的 reasoning
    """
    match = fmt.start_pattern.match(text)
    if not match:
        return ThinkExtractionResult(text, None)

    start = match.end()
    end_pos = text.lower().find(fmt.end_marker.lower(), start)
    if end_pos == -1:
        return ThinkExtractionResult(None, None)

    reasoning = text[start:end_pos]
    after = text[end_pos + len(fmt.end_marker):]
    after = re.sub(r"^[\r\n]{0,2}", "", after)
    return ThinkExtractionResult(after, reasoning)


def _is_incomplete_xml_prefix(
    text: str,
    formats: tuple[ThinkFormat, ...],
) -> bool:
    """检查 *text* 是否可能是未闭合的 XML think 标签前缀。

    用于流式场景：当 chunk 边界切在 XML 开标签中间时，需要识别出这是
    不完整的标签，等待后续 chunk 补充。

    只处理 ``end_marker`` 以 ``</`` 开头的 XML 标签格式；非 XML 格式
    （如 Tibetan Unicode）由 ``start_pattern`` 的单字符匹配保证原子性，
    不需要不完整前缀检测。
    """
    lower = text.lower()

    if not lower.startswith("<"):
        return False

    for fmt in formats:
        # 只处理 XML 标签格式
        if not fmt.end_marker.startswith("</"):
            continue

        # 从 end_marker 推导标签名："</think>" -> "think"
        tag = fmt.end_marker[2:-1]
        prefix = f"<{tag}"

        # text 是 prefix 的前缀（如 <, <t, <th, <thi, <thin）
        if len(lower) < len(prefix):
            if prefix.startswith(lower):
                return True
        # text 以 prefix 开头但没有完整开标签
        elif lower.startswith(prefix):
            if not fmt.start_pattern.match(text):
                # 仅当文本较短且不像自然语言时才视为不完整前缀
                if len(text) <= 30 and " " not in text[len(prefix):]:
                    return True

    return False


def extract_think_prefix(
    text: str | None,
    formats: tuple[ThinkFormat, ...] | None = None,
) -> ThinkExtractionResult:
    """Extract thinking-chain prefix from text.

    支持的格式由 *formats* 参数控制，默认使用内置格式：
    - XML 标签：``<think>...</think>``、``<thinking>...</thinking>``（不区分大小写）
    - Tibetan Unicode：``༺...༽``（DeepSeek 部分模型使用）

    思维链仅当出现在文本最开头时才会被处理；内容中间出现的 think 标签
    （如用户讨论 think 标签语法）不会被误删。

    每种格式只尝试一次，一旦匹配成功立即返回，不再尝试其他格式。

    Args:
        text: 原始文本
        formats: 自定义格式列表，None 使用内置格式

    Returns:
        ThinkExtractionResult:
        - (text, None): 不以 think 格式开头
        - (None, None): 以 think 格式开头但未闭合（不完整）
        - (cleaned, reasoning): 成功提取思维链
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

    # 检查不完整的 XML 开标签（如 <think 没有 >）
    if _is_incomplete_xml_prefix(cleaned, fmts):
        return ThinkExtractionResult(None, None)

    # 不以任何 think 格式开头
    return ThinkExtractionResult(cleaned, None)


def strip_think(text: str | None) -> str | None:
    """去除前缀形式的思维链内容。

    这是 ``extract_think_prefix`` 的便捷包装，只返回清理后的文本。

    支持的格式（均只处理出现在文本最开头的思维链）：
    - XML 标签：``<think>...</think>``、``<thinking>...</thinking>``（不区分大小写）
    - Tibetan Unicode：``༺...༽``（DeepSeek 部分模型使用）
    - 未闭合标签：返回 None（无法判断思维链边界）

    思维链仅当出现在文本最开头时才会被处理；内容中间出现的 think 标签
    （如用户讨论 think 标签语法）不会被误删。

    Args:
        text: 原始文本

    Returns:
        去除思维链后的文本，如果结果为空则返回 None
    """
    result = extract_think_prefix(text)
    return result.cleaned if result.cleaned else None
