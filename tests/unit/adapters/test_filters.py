"""Tests for ContentFilter pipeline (modex_agent.adapters.filters).

Verifies ReasoningContentFilter, WhitespaceFilter,
and ChainedContentFilter behaviors.
"""


from modex_agent.adapters.filters import (
    ChainedContentFilter,
    ContentFilter,
    ReasoningContentFilter,
    WhitespaceFilter,
)
from modex_agent.core.types import OutputMessage


class TestReasoningContentFilter:
    async def test_strip_mode_removes_reasoning(self):
        msg = OutputMessage(content="hello", reasoning="secret")
        f = ReasoningContentFilter(mode="strip")
        result = await f.apply(msg)
        assert result.content == "hello"
        assert result.reasoning is None

    async def test_keep_mode_noop(self):
        msg = OutputMessage(content="hello", reasoning="secret")
        f = ReasoningContentFilter(mode="keep")
        result = await f.apply(msg)
        assert result.content == "hello"
        assert result.reasoning == "secret"


class TestWhitespaceFilter:
    async def test_collapses_excessive_newlines(self):
        msg = OutputMessage(content="a\n\n\n\nb")
        f = WhitespaceFilter()
        result = await f.apply(msg)
        assert result.content == "a\n\nb"

    async def test_strips_edges(self):
        msg = OutputMessage(content="  hello  ")
        f = WhitespaceFilter()
        result = await f.apply(msg)
        assert result.content == "hello"

    async def test_no_strip_when_disabled(self):
        msg = OutputMessage(content="  hello  ")
        f = WhitespaceFilter(strip_edges=False, collapse_lines=False)
        result = await f.apply(msg)
        assert result.content == "  hello  "


class TestChainedContentFilter:
    async def test_runs_filters_in_order(self):
        msg = OutputMessage(content="  hello  ", reasoning="secret")
        chain = ChainedContentFilter([ReasoningContentFilter(), WhitespaceFilter()])
        result = await chain.apply(msg)
        assert result.content == "hello"
        assert result.reasoning is None

    async def test_empty_chain_passthrough(self):
        msg = OutputMessage(content="hello")
        chain = ChainedContentFilter([])
        result = await chain.apply(msg)
        assert result.content == "hello"


class TestContentFilterAbc:
    async def test_custom_filter_subclass(self):
        class UpperFilter(ContentFilter):
            async def apply(self, message: OutputMessage) -> OutputMessage:
                return message.model_copy(update={"content": message.content.upper()})

        msg = OutputMessage(content="hello")
        assert (await UpperFilter().apply(msg)).content == "HELLO"
