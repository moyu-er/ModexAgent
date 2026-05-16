"""Think-tag extractor for streaming and non-streaming LLM responses.

Uses ``ThinkFormat`` from ``framework.utils.helpers`` so that
streaming and non-streaming paths share the same format definitions.
"""

import re
from enum import Enum, auto

from framework.utils.helpers import (
    ThinkExtractionResult,
    ThinkFormat,
    BUILTIN_THINK_FORMATS,
    extract_think_prefix,
)


class _ThinkState(Enum):
    IDLE = auto()
    IN_THINK = auto()
    DONE = auto()


class ThinkTagExtractor:
    """轻量级流式 think-tag 状态机提取器。

    不复用 ``extract_think_prefix`` 的完整扫描逻辑（非流式场景专用），
    而是基于 ``ThinkFormat`` 的定义维护一个轻量状态机：
    - **IDLE**:   等待检测 think 开标签；仅当内容可能以 think 前缀开头时才缓冲
    - **IN_THINK**: 已检测到开标签，累积思维链等待闭标签
    - **DONE**:   提取完成或确认无 think 标签，后续 chunk 直接透传

    推荐用法：每次 LLM 流式调用前新建实例，避免状态跨调用污染。
    """

    # IDLE 状态下的 buffer 上限。足够容纳最长的部分开标签前缀
    #（如 <thinking 为 9 字节），超限后会先尝试匹配再 flush。
    _MAX_IDLE_BUFFER: int = 12

    def __init__(self, formats: tuple[ThinkFormat, ...] | None = None) -> None:
        self._formats = formats if formats is not None else BUILTIN_THINK_FORMATS
        self._state = _ThinkState.IDLE
        self._buffer: str = ""
        self._current_fmt: ThinkFormat | None = None
        self._reasoning: str = ""

    def feed(self, text: str) -> ThinkExtractionResult:
        """Process a new chunk and return (content_delta, reasoning_delta).

        Returns:
            ThinkExtractionResult:
            - (content, None)     : no think prefix; content is clean text
            - (None, None)        : think prefix may be starting; caller
                                    should feed more data
            - (content, reasoning): complete think prefix extracted
        """
        if self._state == _ThinkState.DONE:
            return ThinkExtractionResult(text, None)

        if self._state == _ThinkState.IDLE:
            return self._feed_idle(text)

        # _ThinkState.IN_THINK
        return self._feed_in_think(text)

    # ------------------------------------------------------------------
    # IDLE
    # ------------------------------------------------------------------

    def _feed_idle(self, text: str) -> ThinkExtractionResult:
        combined = self._buffer + text
        stripped = combined.lstrip()

        # 如果 stripped 为空（全是空格），继续缓冲——后续 chunk 可能以 think 开头
        if not stripped:
            self._buffer = combined
            return ThinkExtractionResult(None, None)

        # 快速路径：不以 '<' 或 '༺' 开头 → 肯定不是 think 标签
        if not stripped.startswith("<") and not stripped.startswith("༺"):
            self._state = _ThinkState.DONE
            self._buffer = ""
            return ThinkExtractionResult(combined, None)

        # IDLE buffer 超限检查：先尝试匹配，匹配成功则进入 IN_THINK（不限制）
        if len(combined) > self._MAX_IDLE_BUFFER:
            result = self._try_match_format(stripped)
            if result is not None:
                return result
            # 未匹配且 buffer 超限 → flush 为普通内容
            self._state = _ThinkState.DONE
            self._buffer = ""
            return ThinkExtractionResult(combined, None)

        # 尝试匹配任何格式的 start_pattern
        result = self._try_match_format(stripped)
        if result is not None:
            return result

        # 没有匹配到任何格式
        # 以 '<' 或 '༺' 开头且未超限 → 缓冲等待更多数据（可能是未闭合开标签）
        if stripped.startswith("<") or stripped.startswith("༺"):
            self._buffer = combined
            return ThinkExtractionResult(None, None)

        # 确定不是 think 标签
        self._state = _ThinkState.DONE
        self._buffer = ""
        return ThinkExtractionResult(combined, None)

    def reset(self) -> None:
        """重置状态机，准备处理新的 LLM 响应。"""
        self._state = _ThinkState.IDLE
        self._buffer = ""
        self._current_fmt = None
        self._reasoning = ""

    def flush(self) -> ThinkExtractionResult:
        """Drain buffered IDLE content at end of stream."""
        if self._buffer:
            buf = self._buffer
            self._buffer = ""
            if self._state == _ThinkState.IDLE:
                self._state = _ThinkState.DONE
                return ThinkExtractionResult(buf, None)
        return ThinkExtractionResult(None, None)

    def _try_match_format(self, text: str) -> ThinkExtractionResult | None:
        """尝试用任何格式的 start_pattern 匹配 *text*。

        Returns:
            ThinkExtractionResult if matched (DONE or IN_THINK), None if no format matched.
        """
        for fmt in self._formats:
            match = fmt.start_pattern.match(text)
            if not match:
                continue

            start = match.end()
            end_pos = text.lower().find(fmt.end_marker.lower(), start)
            if end_pos != -1:
                # 开闭标签同在当前 text 中 → 完成提取
                reasoning = text[start:end_pos]
                after = text[end_pos + len(fmt.end_marker):]
                after = re.sub(r"^[\r\n]{0,2}", "", after)
                self._state = _ThinkState.DONE
                self._buffer = ""
                return ThinkExtractionResult(after, reasoning)

            # 只有开标签 → 进入 IN_THINK
            self._state = _ThinkState.IN_THINK
            self._current_fmt = fmt
            self._reasoning = text[start:]
            self._buffer = ""
            return ThinkExtractionResult(None, None)

        return None

    # ------------------------------------------------------------------
    # IN_THINK
    # ------------------------------------------------------------------

    def _feed_in_think(self, text: str) -> ThinkExtractionResult:
        assert self._current_fmt is not None

        combined = self._reasoning + text
        end_pos = combined.lower().find(self._current_fmt.end_marker.lower())

        if end_pos != -1:
            reasoning = combined[:end_pos]
            after = combined[end_pos + len(self._current_fmt.end_marker):]
            after = re.sub(r"^[\r\n]{0,2}", "", after)
            self._state = _ThinkState.DONE
            self._reasoning = ""
            return ThinkExtractionResult(after, reasoning)

        # Close tag not found yet, keep accumulating
        self._reasoning = combined
        return ThinkExtractionResult(None, None)

    # ------------------------------------------------------------------
    # Non-streaming
    # ------------------------------------------------------------------

    @classmethod
    def extract(cls, text: str) -> ThinkExtractionResult:
        """One-shot extraction for complete non-streaming text."""
        return extract_think_prefix(text)
