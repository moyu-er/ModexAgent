"""ContentFilter pipeline for OutputAdapter.

Provides pluggable content filtering before messages are sent
to the target platform (QQ, CLI, HTTP, etc.).
"""

import re
from abc import ABC, abstractmethod
from ..core.types import OutputMessage


def _get_think_tag_extractor():
    from ..extensions.llm.litellm_provider import ThinkTagExtractor
    return ThinkTagExtractor


class ContentFilter(ABC):
    """Abstract base class for content filters."""

    @abstractmethod
    async def apply(self, message: OutputMessage) -> OutputMessage:
        """Apply filtering to an output message and return the modified message."""
        pass


class ChainedContentFilter(ContentFilter):
    """Chain multiple filters together."""

    def __init__(self, filters: list[ContentFilter]):
        self.filters = filters

    async def apply(self, message: OutputMessage) -> OutputMessage:
        for f in self.filters:
            message = await f.apply(message)
        return message


class ThinkTagFilter(ContentFilter):
    """Remove <think>...</think> tags from content.

    Optionally extracts the hidden reasoning and stores it in
    ``message.reasoning``.
    """

    def __init__(self, extract_reasoning: bool = True):
        self.extract_reasoning = extract_reasoning

    async def apply(self, message: OutputMessage) -> OutputMessage:
        if not message.content:
            return message
        clean_content, reasoning = _get_think_tag_extractor().extract(message.content)
        message.content = clean_content
        if self.extract_reasoning and reasoning:
            existing = message.reasoning or ""
            message.reasoning = existing + reasoning
        return message


class ReasoningContentFilter(ContentFilter):
    """Control visibility of reasoning content.

    Modes:
        - ``strip``:  remove reasoning entirely
        - ``append``: prepend reasoning to content (as <think> tags) and clear reasoning
        - ``keep``:   do nothing
    """

    def __init__(self, mode: str = "strip"):
        self.mode = mode

    async def apply(self, message: OutputMessage) -> OutputMessage:
        if self.mode == "strip":
            message.reasoning = None
        elif self.mode == "append" and message.reasoning:
            think_block = f"<think>{message.reasoning}</think>\n\n"
            message.content = think_block + (message.content or "")
            message.reasoning = None
        # "keep" mode: no-op
        return message


class WhitespaceFilter(ContentFilter):
    """Clean up excessive whitespace."""

    def __init__(self, collapse_lines: bool = True, strip_edges: bool = True):
        self.collapse_lines = collapse_lines
        self.strip_edges = strip_edges

    async def apply(self, message: OutputMessage) -> OutputMessage:
        if not message.content:
            return message
        content = message.content
        if self.collapse_lines:
            content = re.sub(r"\n{3,}", "\n\n", content)
        if self.strip_edges:
            content = content.strip()
        message.content = content
        return message
