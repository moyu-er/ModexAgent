"""Tests for ThinkTagExtractor streaming parser.

验证 ThinkTagExtractor 正确解析流式 think 标签：
- 前缀-only 语义（只处理开头的 think 标签）
- 完整标签在单个 chunk 中
- 标签跨多个 chunks
- 没有 think 标签
- 多种格式：<think>、<thinking>、Tibetan Unicode
- 大小写不敏感
- 64KB 缓冲区保护
"""

import pytest

from framework.utils.think_tag import ThinkTagExtractor


class TestThinkTagExtractor:
    """ThinkTagExtractor unit tests."""

    @pytest.fixture
    def extractor(self):
        return ThinkTagExtractor()

    # -- Prefix-only semantics ------------------------------------------------

    def test_think_tag_at_prefix(self, extractor):
        """Think tag at the start of content is extracted."""
        content, reasoning = extractor.feed("<think>reasoning</think> hello")
        assert content == " hello"
        assert reasoning == "reasoning"

    def test_think_tag_not_at_prefix_ignored(self, extractor):
        """Think tag in the middle of content is NOT extracted (prefix-only)."""
        content, reasoning = extractor.feed("Hello <think>reasoning</think> world")
        assert content == "Hello <think>reasoning</think> world"
        assert reasoning is None

    # -- Multiple formats -----------------------------------------------------

    def test_thinking_tag_at_prefix(self, extractor):
        """<thinking> tag at prefix is extracted."""
        content, reasoning = extractor.feed("<thinking>reason</thinking> hello")
        assert content == " hello"
        assert reasoning == "reason"

    def test_tibetan_unicode_at_prefix(self, extractor):
        """Tibetan Unicode markers at prefix are extracted."""
        content, reasoning = extractor.feed("༺reason༽ hello")
        assert content == " hello"
        assert reasoning == "reason"

    def test_case_insensitive_think(self, extractor):
        """Uppercase <THINK> at prefix is extracted."""
        content, reasoning = extractor.feed("<THINK>reason</THINK> hello")
        assert content == " hello"
        assert reasoning == "reason"

    def test_case_insensitive_thinking(self, extractor):
        """Mixed-case <Thinking> at prefix is extracted."""
        content, reasoning = extractor.feed("<Thinking>reason</Thinking> hello")
        assert content == " hello"
        assert reasoning == "reason"

    # -- Streaming chunk boundaries -------------------------------------------

    def test_think_tag_split_across_chunks(self, extractor):
        """Think tag spanning multiple chunks — prefix-only."""
        c1, r1 = extractor.feed("<think>")
        assert c1 is None
        assert r1 is None

        c2, r2 = extractor.feed("reason")
        assert c2 is None
        assert r2 is None

        c3, r3 = extractor.feed("</think> world")
        assert c3 == " world"
        assert r3 == "reason"

    def test_no_think_tag(self, extractor):
        """Content without think tags passes through immediately."""
        content, reasoning = extractor.feed("Just normal content")
        assert content == "Just normal content"
        assert reasoning is None

    def test_empty_think_tag(self, extractor):
        """Empty think tag."""
        content, reasoning = extractor.feed("<think></think> After")
        assert content == " After"
        assert reasoning == ""

    def test_only_think_tag(self, extractor):
        """Content that is only a think tag."""
        content, reasoning = extractor.feed("<think>Only reasoning</think>")
        assert content == ""
        assert reasoning == "Only reasoning"

    def test_unclosed_think_tag_buffers(self, extractor):
        """Unclosed think tag causes buffering until closed."""
        c1, r1 = extractor.feed("<think>ongoing")
        assert c1 is None
        assert r1 is None

        c2, r2 = extractor.feed(" still")
        assert c2 is None
        assert r2 is None

        c3, r3 = extractor.feed(" more</think> done")
        assert c3 == " done"
        assert r3 == "ongoing still more"

    def test_done_state_passes_through(self, extractor):
        """After completion, subsequent chunks pass through."""
        extractor.feed("<think>r</think> A ")
        c2, r2 = extractor.feed("B ")
        assert c2 == "B "
        assert r2 is None

        c3, r3 = extractor.feed("C")
        assert c3 == "C"
        assert r3 is None

    # -- Buffer safety --------------------------------------------------------

    def test_idle_buffer_limit_flush(self, extractor):
        """IDLE 状态下非 think 内容超过 buffer 限制时直接 flush。"""
        large_text = "x" * 500
        content, reasoning = extractor.feed(large_text)
        assert content == large_text
        assert reasoning is None

    def test_in_think_no_buffer_limit(self, extractor):
        """IN_THINK 状态下 reasoning 可以很大。"""
        extractor.feed("<think>")
        large_reasoning = "x" * (65 * 1024)
        c2, r2 = extractor.feed(large_reasoning)
        assert c2 is None
        assert r2 is None

    # -- Leading whitespace ----------------------------------------------------

    def test_leading_whitespace_then_think_tag(self, extractor):
        """Whitespace-only chunk buffers, think tag in next chunk."""
        c1, r1 = extractor.feed("   ")
        assert c1 is None
        assert r1 is None

        c2, r2 = extractor.feed("<think>r</think> hello")
        assert c2 == " hello"
        assert r2 == "r"

    def test_leading_whitespace_no_think(self, extractor):
        """Whitespace-only chunk, then normal content. Whitespace preserved."""
        c1, r1 = extractor.feed("  ")
        assert c1 is None
        assert r1 is None

        c2, r2 = extractor.feed("Hello world")
        assert c2 == "  Hello world"
        assert r2 is None

    # -- Think tag with attributes ---------------------------------------------

    def test_think_tag_with_attributes(self, extractor):
        """<think type='reasoning'> with attributes."""
        content, reasoning = extractor.feed(
            "<think type='reasoning'>deep thought</think> result"
        )
        assert content == " result"
        assert reasoning == "deep thought"

    def test_thinking_tag_with_attributes(self, extractor):
        """<thinking attr='x'> with attributes."""
        content, reasoning = extractor.feed(
            "<thinking attr='x' b='y'>thought</thinking> result"
        )
        assert content == " result"
        assert reasoning == "thought"

    # -- Chunk boundaries ------------------------------------------------------

    def test_think_open_tag_split_with_attributes(self, extractor):
        """Open tag with attributes split across chunks."""
        c1, r1 = extractor.feed("<think")
        assert c1 is None
        assert r1 is None

        c2, r2 = extractor.feed(" type='x'>")
        assert c2 is None
        assert r2 is None

        c3, r3 = extractor.feed("reason</think> done")
        assert c3 == " done"
        assert r3 == "reason"

    def test_thinking_tag_split_across_chunks(self, extractor):
        """<thinking> tag spanning chunks."""
        c1, r1 = extractor.feed("<thi")
        assert c1 is None
        assert r1 is None

        c2, r2 = extractor.feed("nking>thought</thinking> result")
        assert c2 == " result"
        assert r2 == "thought"

    def test_open_tag_without_gt_buffers(self, extractor):
        """'<think' without closing '>' — incomplete, keep buffering."""
        c1, r1 = extractor.feed("<think")
        assert c1 is None
        assert r1 is None

        c2, r2 = extractor.feed(">x</think> y")
        assert c2 == " y"
        assert r2 == "x"

    # -- HTML-like non-think content -------------------------------------------

    def test_html_div_not_a_think_tag(self, extractor):
        """<div> is not a think tag — passes through."""
        content, reasoning = extractor.feed("<div>hello</div>")
        assert content == "<div>hello</div>"
        assert reasoning is None

    def test_html_tag_in_chunks_flushed_on_overflow(self, extractor):
        """<d buffered (starts with <), overflow flushes combined content."""
        c1, r1 = extractor.feed("<d")
        assert c1 is None  # starts with <, buffered until overflow
        assert r1 is None

        c2, r2 = extractor.feed("iv>hello</div>")
        assert c2 == "<div>hello</div>"
        assert r2 is None

    # -- Extractor instance isolation ------------------------------------------

    def test_fresh_instance_per_call(self):
        """Each new instance has clean state."""
        e1 = ThinkTagExtractor()
        e1.feed("<think>r</think> hello")
        # e1 is now DONE

        e2 = ThinkTagExtractor()
        c, r = e2.feed("<think>new</think> world")
        assert c == " world"
        assert r == "new"

    def test_same_instance_reused_without_reset_leaks_state(self):
        """Without reset, a DONE extractor passes everything through."""
        e = ThinkTagExtractor()
        e.feed("<think>first</think> A")

        # Second stream with same extractor — DONE state persists
        c, r = e.feed("<think>second</think> B")
        assert c == "<think>second</think> B"  # leaks through!
        assert r is None

    # -- Content immediately after close tag -----------------------------------

    def test_content_right_after_close(self, extractor):
        """No newline between </think> and content."""
        content, reasoning = extractor.feed("<think>r</think>immediate")
        assert content == "immediate"
        assert reasoning == "r"

    def test_newline_after_close_stripped(self, extractor):
        """Newline after </think> is stripped."""
        content, reasoning = extractor.feed("<think>r</think>\nhello")
        assert content == "hello"
        assert reasoning == "r"

    # -- flush() ---------------------------------------------------------------

    def test_flush_drains_buffered_idle_content(self):
        """flush() returns buffered IDLE content that never matched."""
        e = ThinkTagExtractor()
        e.feed("<div")  # IDLE, starts with <, buffered
        c2, r2 = e.feed(">")  # <div> complete, still under buffer, no match → buffered
        assert c2 is None

        flushed, r = e.flush()
        assert flushed == "<div>"
        assert r is None

    def test_flush_empty_when_nothing_buffered(self):
        """flush() is a no-op when nothing is buffered."""
        e = ThinkTagExtractor()
        e.feed("<think>r</think> done")  # DONE, nothing buffered
        flushed, r = e.flush()
        assert flushed is None
        assert r is None

    # -- reset() ----------------------------------------------------------------

    def test_reset_clears_state(self):
        """reset() allows reuse of an extractor instance."""
        e = ThinkTagExtractor()
        e.feed("<think>first</think> A")
        # e is now DONE

        e.reset()
        c, r = e.feed("<think>second</thinking> B")
        assert c == " B"
        assert r == "second"

    # -- Non-streaming extract() -----------------------------------------------

    def test_extract_classmethod_complete(self, extractor):
        """One-shot extract method with think tag."""
        content, reasoning = ThinkTagExtractor.extract("<think>r</think> hello")
        assert content == " hello"
        assert reasoning == "r"

    def test_extract_classmethod_no_think(self, extractor):
        """One-shot extract without think tag."""
        content, reasoning = ThinkTagExtractor.extract("hello world")
        assert content == "hello world"
        assert reasoning is None

    def test_extract_classmethod_empty_result(self, extractor):
        """One-shot extract where think tag consumes all content."""
        content, reasoning = ThinkTagExtractor.extract("<think>r</think>")
        assert content == ""
        assert reasoning == "r"
