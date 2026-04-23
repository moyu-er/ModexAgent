"""Tests for ThinkTagExtractor streaming parser.

验证 ThinkTagExtractor 正确解析流式 think 标签：
- 完整标签在单个 chunk 中
- 标签跨多个 chunks
- 没有 think 标签
- 部分 </think> 在边界
- 空的 think 标签
"""

import pytest

from framework.extensions.llm.litellm_provider import ThinkTagExtractor


class TestThinkTagExtractor:
    """ThinkTagExtractor unit tests."""

    @pytest.fixture
    def extractor(self):
        return ThinkTagExtractor()

    def test_single_chunk_complete_think_tag(self, extractor):
        """Test extracting think tag from a single chunk."""
        content, reasoning = extractor.feed("Hello <think>reasoning</think> world")
        assert content == "Hello  world"
        assert reasoning == "reasoning"

    def test_no_think_tag(self, extractor):
        """Test that content without think tags passes through."""
        content, reasoning = extractor.feed("Just normal content")
        assert content == "Just normal content"
        assert reasoning is None

    def test_think_tag_split_across_chunks(self, extractor):
        """Test think tag split across multiple chunks."""
        c1, r1 = extractor.feed("<think>rea")
        assert c1 is None or c1 == ""
        assert r1 == "rea"

        c2, r2 = extractor.feed("soning</th")
        # partial </think> at end should be buffered
        assert c2 is None or c2 == ""
        assert r2 == "soning"

        c3, r3 = extractor.feed("ink> world")
        assert c3 == " world"
        assert r3 is None or r3 == ""

    def test_multiple_think_tags(self, extractor):
        """Test handling multiple think tags in content."""
        content, reasoning = extractor.feed("A <think>r1</think> B <think>r2</think> C")
        assert content == "A  B  C"
        assert reasoning == "r1r2"

    def test_empty_think_tag(self, extractor):
        """Test empty think tag."""
        content, reasoning = extractor.feed("Before <think></think> After")
        assert content == "Before  After"
        assert reasoning is None or reasoning == ""

    def test_only_think_tag(self, extractor):
        """Test content that is only a think tag."""
        content, reasoning = extractor.feed("<think>Only reasoning</think>")
        assert content is None or content == ""
        assert reasoning == "Only reasoning"

    def test_partial_close_tag_at_boundary(self, extractor):
        """Test partial </think> spanning chunk boundary."""
        c1, r1 = extractor.feed("<think>abc</thi")
        # The </thi should not be emitted as reasoning yet
        assert c1 is None or c1 == ""
        assert r1 == "abc"

        c2, r2 = extractor.feed("nk> rest")
        assert c2 == " rest"
        assert r2 is None or r2 == ""

    def test_state_reset(self, extractor):
        """Test that extractor can be reused after completion."""
        c1, _ = extractor.feed("<think>r1</think> A ")
        assert c1 == " A "
        content, reasoning = extractor.feed("B <think>r2</think> C")
        assert content == "B  C"
        assert reasoning == "r2"

    def test_open_without_close_insidethink(self, extractor):
        """Test chunk that opens think but never closes (extractor stays in think mode)."""
        c1, r1 = extractor.feed("<think>ongoing")
        assert c1 is None or c1 == ""
        assert r1 == "ongoing"

        c2, r2 = extractor.feed(" still")
        assert c2 is None or c2 == ""
        assert r2 == " still"

    def test_multiple_feeds_after_close(self, extractor):
        """Test multiple feeds after a think tag is closed."""
        extractor.feed("<think>r</think> A ")
        c2, r2 = extractor.feed("B ")
        assert c2 == "B "
        assert r2 is None or r2 == ""

        c3, r3 = extractor.feed("C")
        assert c3 == "C"
        assert r3 is None or r3 == ""
