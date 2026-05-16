"""Think-tag extractor for streaming and non-streaming LLM responses.

Uses ``ThinkFormat`` from ``framework.utils.helpers`` so that
streaming and non-streaming paths share the same format definitions.
"""

from enum import Enum, auto

from framework.utils.helpers import (
    ThinkExtractionResult,
    ThinkFormat,
    BUILTIN_THINK_FORMATS,
    extract_think_prefix,
    looks_like_prefix,
)


class _ThinkState(Enum):
    IDLE = auto()
    IN_THINK = auto()
    DONE = auto()


class ThinkTagExtractor:
    """Lightweight streaming think-tag state machine.

    Only ambiguous fragments (partial open/close tags that could affect
    state transitions) are buffered.  All other content is returned
    immediately as content or reasoning deltas for typewriter-style output.
    """

    _MAX_IDLE_BUFFER: int = 20

    def __init__(self, formats: tuple[ThinkFormat, ...] | None = None) -> None:
        self._formats = formats if formats is not None else BUILTIN_THINK_FORMATS
        self._state = _ThinkState.IDLE
        self._buffer: str = ""
        self._current_fmt: ThinkFormat | None = None
        self._pending: str = ""

    def feed(self, text: str) -> ThinkExtractionResult:
        if self._state == _ThinkState.DONE:
            return ThinkExtractionResult(text, None)
        if self._state == _ThinkState.IDLE:
            return self._feed_idle(text)
        return self._feed_in_think(text)

    # ------------------------------------------------------------------
    # IDLE
    # ------------------------------------------------------------------

    def _feed_idle(self, text: str) -> ThinkExtractionResult:
        combined = self._buffer + text
        stripped = combined.lstrip()

        if not stripped:
            self._buffer = combined
            return ThinkExtractionResult(None, None)

        if not self._starts_like_any_format(stripped):
            self._state = _ThinkState.DONE
            self._buffer = ""
            return ThinkExtractionResult(combined, None)

        if len(combined) > self._MAX_IDLE_BUFFER:
            result = self._try_match_format(stripped)
            if result is not None:
                return result
            self._state = _ThinkState.DONE
            self._buffer = ""
            return ThinkExtractionResult(combined, None)

        result = self._try_match_format(stripped)
        if result is not None:
            return result

        if looks_like_prefix(stripped, self._formats):
            self._buffer = combined
            return ThinkExtractionResult(None, None)

        self._state = _ThinkState.DONE
        self._buffer = ""
        return ThinkExtractionResult(combined, None)

    def _starts_like_any_format(self, text: str) -> bool:
        ch = text[0]
        for fmt in self._formats:
            if fmt.open_literal[0] == ch:
                return True
        return False

    def _try_match_format(self, text: str) -> ThinkExtractionResult | None:
        lower = text.lower()

        for fmt in self._formats:
            open_lower = fmt.open_literal.lower()
            if not lower.startswith(open_lower):
                continue

            start = len(fmt.open_literal)
            end_pos = lower.find(fmt.end_marker.lower(), start)
            if end_pos != -1:
                reasoning = text[start:end_pos]
                after = text[end_pos + len(fmt.end_marker):]
                after = self._trim_nl(after)
                self._state = _ThinkState.DONE
                self._buffer = ""
                return ThinkExtractionResult(after, reasoning)

            # Open matched, close not found → IN_THINK
            after_open = text[start:]
            suffix = _close_tag_suffix(after_open, fmt.end_marker)
            if suffix:
                delta = after_open[:-len(suffix)] if len(after_open) > len(suffix) else None
                self._pending = suffix
            else:
                delta = after_open
                self._pending = ""

            self._state = _ThinkState.IN_THINK
            self._current_fmt = fmt
            self._buffer = ""
            return ThinkExtractionResult(None, delta)

        return None

    # ------------------------------------------------------------------
    # IN_THINK
    # ------------------------------------------------------------------

    def _feed_in_think(self, text: str) -> ThinkExtractionResult:
        assert self._current_fmt is not None
        end_marker = self._current_fmt.end_marker

        combined = self._pending + text
        end_pos = combined.lower().find(end_marker.lower())

        if end_pos != -1:
            before_close = combined[:end_pos]
            after = combined[end_pos + len(end_marker):]
            after = self._trim_nl(after)
            self._state = _ThinkState.DONE
            self._pending = ""
            return ThinkExtractionResult(after or None, before_close)

        suffix = _close_tag_suffix(combined, end_marker)
        if suffix:
            safe = combined[:-len(suffix)] if len(combined) > len(suffix) else ""
            self._pending = suffix
            return ThinkExtractionResult(None, safe or None)

        self._pending = ""
        return ThinkExtractionResult(None, text)

    @staticmethod
    def _trim_nl(text: str) -> str:
        if text.startswith("\r\n"):
            return text[2:]
        if text.startswith("\n") or text.startswith("\r"):
            return text[1:]
        return text

    def flush(self) -> ThinkExtractionResult:
        if self._buffer:
            buf = self._buffer
            self._buffer = ""
            if self._state == _ThinkState.IDLE:
                self._state = _ThinkState.DONE
                return ThinkExtractionResult(buf, None)
        return ThinkExtractionResult(None, None)

    # ------------------------------------------------------------------
    # Non-streaming
    # ------------------------------------------------------------------

    @classmethod
    def extract(cls, text: str) -> ThinkExtractionResult:
        return extract_think_prefix(text)


def _close_tag_suffix(text: str, end_marker: str) -> str:
    """Return the longest suffix of *text* that is a prefix of *end_marker*."""
    end_lower = end_marker.lower()
    text_lower = text.lower()
    max_check = min(len(text), len(end_marker) - 1)
    for i in range(max_check, 0, -1):
        suffix = text[-i:]
        if end_lower.startswith(suffix.lower()):
            return suffix
    return ""
