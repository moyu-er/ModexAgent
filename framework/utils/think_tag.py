"""Think-tag extractor for streaming and non-streaming LLM responses.

Strips ``...`` reasoning blocks from model output, supporting
incomplete tags that span chunk boundaries during streaming.
Extracted from litellm_provider.py for reuse by other providers.
"""


class ThinkTagExtractor:
    """Stateful streaming think-tag extractor.

    Splits incoming text chunks into visible content and hidden reasoning.
    Supports incomplete tags spanning chunk boundaries.
    """

    _OPEN_TAG = "nope"
    _CLOSE_TAG = "nope"

    def __init__(self):
        self._in_think = False
        self._pending = ""

    def feed(self, text: str) -> tuple[str | None, str | None]:
        """Process a new chunk and return (content_delta, reasoning_delta)."""
        data = self._pending + text
        self._pending = ""
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        i = 0
        n = len(data)

        while i < n:
            if self._in_think:
                close_idx = data.find(self._CLOSE_TAG, i)
                if close_idx != -1:
                    reasoning_parts.append(data[i:close_idx])
                    self._in_think = False
                    i = close_idx + len(self._CLOSE_TAG)
                    continue

                remaining = data[i:]
                max_prefix = min(len(remaining), len(self._CLOSE_TAG) - 1)
                prefix_len = 0
                for k in range(max_prefix, 0, -1):
                    if remaining.endswith(self._CLOSE_TAG[:k]):
                        prefix_len = k
                        break
                if prefix_len:
                    keep_len = len(remaining) - prefix_len
                    if keep_len > 0:
                        reasoning_parts.append(remaining[:keep_len])
                    self._pending = remaining[keep_len:]
                    i = n
                else:
                    reasoning_parts.append(remaining)
                    i = n
            else:
                open_idx = data.find(self._OPEN_TAG, i)
                if open_idx != -1:
                    content_parts.append(data[i:open_idx])
                    self._in_think = True
                    i = open_idx + len(self._OPEN_TAG)
                    continue

                remaining = data[i:]
                max_prefix = min(len(remaining), len(self._OPEN_TAG) - 1)
                prefix_len = 0
                for k in range(max_prefix, 0, -1):
                    if remaining.endswith(self._OPEN_TAG[:k]):
                        prefix_len = k
                        break
                if prefix_len:
                    keep_len = len(remaining) - prefix_len
                    if keep_len > 0:
                        content_parts.append(remaining[:keep_len])
                    self._pending = remaining[keep_len:]
                    i = n
                else:
                    content_parts.append(remaining)
                    i = n

        content = "".join(content_parts) if content_parts else None
        reasoning = "".join(reasoning_parts) if reasoning_parts else None
        return content, reasoning

    @classmethod
    def extract(cls, text: str) -> tuple[str, str | None]:
        """One-shot extraction for complete non-streaming text."""
        extractor = cls()
        content, reasoning = extractor.feed(text)
        return content or "", reasoning
