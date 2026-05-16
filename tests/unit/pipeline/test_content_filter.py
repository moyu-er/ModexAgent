"""Tests for ContentFilter pipeline.

TDD: verify ReasoningContentFilter, WhitespaceFilter,
and ChainedContentFilter behaviors.
"""

import pytest

from framework.core.types import OutputMessage
from framework.pipeline.filters import (
    ChainedContentFilter,
    ContentFilter,
    ReasoningContentFilter,
    WhitespaceFilter,
)


class TestReasoningContentFilter:
    @pytest.mark.asyncio
    async def test_strip_mode_removes_reasoning(self):
        msg = OutputMessage(content="hello", reasoning="secret")
        f = ReasoningContentFilter(mode="strip")
        result = await f.apply(msg)
        assert result.content == "hello"
        assert result.reasoning is None

    @pytest.mark.asyncio
    async def test_keep_mode_noop(self):
        msg = OutputMessage(content="hello", reasoning="secret")
        f = ReasoningContentFilter(mode="keep")
        result = await f.apply(msg)
        assert result.content == "hello"
        assert result.reasoning == "secret"


class TestWhitespaceFilter:
    @pytest.mark.asyncio
    async def test_collapses_excessive_newlines(self):
        msg = OutputMessage(content="a\n\n\n\nb")
        f = WhitespaceFilter()
        result = await f.apply(msg)
        assert result.content == "a\n\nb"

    @pytest.mark.asyncio
    async def test_strips_edges(self):
        msg = OutputMessage(content="  hello  ")
        f = WhitespaceFilter()
        result = await f.apply(msg)
        assert result.content == "hello"

    @pytest.mark.asyncio
    async def test_no_strip_when_disabled(self):
        msg = OutputMessage(content="  hello  ")
        f = WhitespaceFilter(strip_edges=False, collapse_lines=False)
        result = await f.apply(msg)
        assert result.content == "  hello  "


class TestChainedContentFilter:
    @pytest.mark.asyncio
    async def test_runs_filters_in_order(self):
        msg = OutputMessage(content="  hello  ", reasoning="secret")
        chain = ChainedContentFilter([ReasoningContentFilter(), WhitespaceFilter()])
        result = await chain.apply(msg)
        assert result.content == "hello"
        assert result.reasoning is None

    @pytest.mark.asyncio
    async def test_empty_chain_passthrough(self):
        msg = OutputMessage(content="hello")
        chain = ChainedContentFilter([])
        result = await chain.apply(msg)
        assert result.content == "hello"
