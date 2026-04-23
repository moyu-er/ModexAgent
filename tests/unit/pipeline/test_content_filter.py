"""Tests for ContentFilter pipeline.

TDD: verify ThinkTagFilter, ReasoningContentFilter, WhitespaceFilter,
and ChainedContentFilter behaviors.
"""

import pytest

from framework.core.types import OutputMessage
from framework.pipeline.filters import (
    ChainedContentFilter,
    ContentFilter,
    ReasoningContentFilter,
    ThinkTagFilter,
    WhitespaceFilter,
)


class TestThinkTagFilter:
    @pytest.mark.asyncio
    async def test_extracts_reasoning_and_cleans_content(self):
        msg = OutputMessage(content="Hello <think>reasoning</think> world")
        f = ThinkTagFilter()
        result = await f.apply(msg)
        assert result.content == "Hello  world"
        assert result.reasoning == "reasoning"

    @pytest.mark.asyncio
    async def test_no_think_tag_passthrough(self):
        msg = OutputMessage(content="Hello world")
        f = ThinkTagFilter()
        result = await f.apply(msg)
        assert result.content == "Hello world"
        assert result.reasoning is None

    @pytest.mark.asyncio
    async def test_multiple_think_tags_concat_reasoning(self):
        msg = OutputMessage(content="<think>A</think> x <think>B</think>")
        f = ThinkTagFilter()
        result = await f.apply(msg)
        assert result.content == " x "
        assert result.reasoning == "AB"

    @pytest.mark.asyncio
    async def test_preserves_existing_reasoning(self):
        msg = OutputMessage(content="<think>new</think>", reasoning="old")
        f = ThinkTagFilter()
        result = await f.apply(msg)
        assert result.reasoning == "oldnew"

    @pytest.mark.asyncio
    async def test_extract_reasoning_false(self):
        msg = OutputMessage(content="<think>r</think>text")
        f = ThinkTagFilter(extract_reasoning=False)
        result = await f.apply(msg)
        assert result.content == "text"
        assert result.reasoning is None


class TestReasoningContentFilter:
    @pytest.mark.asyncio
    async def test_strip_mode_removes_reasoning(self):
        msg = OutputMessage(content="hello", reasoning="secret")
        f = ReasoningContentFilter(mode="strip")
        result = await f.apply(msg)
        assert result.content == "hello"
        assert result.reasoning is None

    @pytest.mark.asyncio
    async def test_append_mode_prepends_reasoning(self):
        msg = OutputMessage(content="hello", reasoning="secret")
        f = ReasoningContentFilter(mode="append")
        result = await f.apply(msg)
        assert result.content == "<think>secret</think>\n\nhello"
        assert result.reasoning is None

    @pytest.mark.asyncio
    async def test_keep_mode_noop(self):
        msg = OutputMessage(content="hello", reasoning="secret")
        f = ReasoningContentFilter(mode="keep")
        result = await f.apply(msg)
        assert result.content == "hello"
        assert result.reasoning == "secret"

    @pytest.mark.asyncio
    async def test_append_without_reasoning(self):
        msg = OutputMessage(content="hello")
        f = ReasoningContentFilter(mode="append")
        result = await f.apply(msg)
        assert result.content == "hello"


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
        msg = OutputMessage(content="<think>r</think>  hello  ")
        chain = ChainedContentFilter([ThinkTagFilter(), WhitespaceFilter()])
        result = await chain.apply(msg)
        assert result.content == "hello"
        assert result.reasoning == "r"

    @pytest.mark.asyncio
    async def test_empty_chain_passthrough(self):
        msg = OutputMessage(content="hello")
        chain = ChainedContentFilter([])
        result = await chain.apply(msg)
        assert result.content == "hello"
