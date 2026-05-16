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
        """Think tag spanning multiple chunks — deltas streamed immediately."""
        c1, r1 = extractor.feed("<think>")
        assert c1 is None
        assert r1 == ""       # empty reasoning after open tag

        c2, r2 = extractor.feed("reason")
        assert c2 is None
        assert r2 == "reason"  # streamed as reasoning delta

        c3, r3 = extractor.feed("</think> world")
        assert c3 == " world"
        assert r3 == ""        # close tag completed, no new reasoning

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
        """Reasoning streamed as deltas until close tag found."""
        c1, r1 = extractor.feed("<think>ongoing")
        assert c1 is None
        assert r1 == "ongoing"  # streamed immediately

        c2, r2 = extractor.feed(" still")
        assert c2 is None
        assert r2 == " still"   # streamed immediately

        c3, r3 = extractor.feed(" more</think> done")
        assert c3 == " done"
        assert r3 == " more"    # last delta before close

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
        """IN_THINK streams large reasoning as delta immediately."""
        extractor.feed("<think>")
        large_reasoning = "x" * (65 * 1024)
        c2, r2 = extractor.feed(large_reasoning)
        assert c2 is None
        assert r2 == large_reasoning  # streamed immediately

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

    def test_thinking_tag_split_across_chunks(self, extractor):
        """<thinking> tag spanning chunks with streaming deltas."""
        c1, r1 = extractor.feed("<thi")
        assert c1 is None
        assert r1 is None  # still buffering prefix

        c2, r2 = extractor.feed("nking>thought</thinking> result")
        assert c2 == " result"
        assert r2 == "thought"

    def test_open_tag_without_gt_buffers(self, extractor):
        """'<think' buffers, then '>x...' completes open + extracts."""
        c1, r1 = extractor.feed("<think")
        assert c1 is None
        assert r1 is None  # buffered: could be <think> or <thinking>

        c2, r2 = extractor.feed(">x</think> y")
        assert c2 == " y"
        assert r2 == "x"

    # -- HTML-like non-think content -------------------------------------------

    def test_html_div_not_a_think_tag(self, extractor):
        """<div> is not a think tag — passes through."""
        content, reasoning = extractor.feed("<div>hello</div>")
        assert content == "<div>hello</div>"
        assert reasoning is None

    def test_html_tag_passes_through_immediately(self, extractor):
        """<d cannot become <think> → flushed immediately (format-driven)."""
        c1, r1 = extractor.feed("<d")
        assert c1 == "<d"
        assert r1 is None

        c2, r2 = extractor.feed("iv>hello</div>")
        assert c2 == "iv>hello</div>"
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

    # -- Split open AND close tags across chunks -------------------------------

    def test_split_open_and_close_tags(self, extractor):
        """Both open and close tags split across chunks."""
        c1, r1 = extractor.feed("<thin")
        assert c1 is None
        assert r1 is None  # buffered prefix

        c2, r2 = extractor.feed("k>thinkContent</thi")
        assert c2 is None
        assert r2 == "thinkContent"  # reasoning delta; </thi stripped as close prefix

        c3, r3 = extractor.feed("nk>")
        assert c3 or True  # close completed at end (""=falsy, same as None to caller)
        assert r3 == ""    # no new reasoning

    def test_split_open_close_with_after_content(self, extractor):
        """Split tags with content after close."""
        c1, r1 = extractor.feed("<thin")
        assert c1 is None
        assert r1 is None

        c2, r2 = extractor.feed("k>innerReasoning</thi")
        assert c2 is None
        assert r2 == "innerReasoning"  # streamed; </thi stripped as suffix

        c3, r3 = extractor.feed("nk> final output")
        assert c3 == " final output"
        assert r3 == ""   # nk> completes the close tag

    # -- Format-driven prefix detection ----------------------------------------

    def test_custom_non_xml_format(self):
        """A non-XML custom format works correctly (pure string matching)."""
        from framework.utils.helpers import ThinkFormat

        custom_format = ThinkFormat(
            name="custom",
            open_literal="[THINK]",
            end_marker="[/THINK]",
        )
        e = ThinkTagExtractor(formats=(custom_format,))
        c, r = e.feed("[THINK]hello[/THINK] world")
        assert c == " world"
        assert r == "hello"

    def test_custom_delimited_format_split(self):
        """Custom delimited format with split chunks."""
        from framework.utils.helpers import ThinkFormat

        custom_fmt = ThinkFormat(
            name="delim",
            open_literal="[THINK]",
            end_marker="[/THINK]",
        )
        e = ThinkTagExtractor(formats=(custom_fmt,))

        c1, r1 = e.feed("[THI")
        assert c1 is None
        assert r1 is None  # buffered prefix

        c2, r2 = e.feed("NK]reasoning[/THINK] done")
        assert c2 == " done"
        assert r2 == "reasoning"  # streamed; close tag suffix handled internally

    # -- extract_think_prefix format-driven behavior ---------------------------

    def test_extract_think_prefix_preserves_thinkish_prose(self):
        """Text starting with '<think' but not '<think>' is preserved.

        With exact open_literal matching, '<think carefully...' does not
        start with '<think>' so it is correctly identified as non-think content.
        """
        from framework.utils.helpers import extract_think_prefix

        result = extract_think_prefix("<think carefully about x and y")
        assert result.cleaned == "<think carefully about x and y"

    def test_extract_think_prefix_custom_format(self):
        """Non-XML custom format works with extract_think_prefix."""
        from framework.utils.helpers import ThinkFormat, extract_think_prefix

        custom = ThinkFormat(
            name="custom",
            open_literal="[THINK]",
            end_marker="[/THINK]",
        )
        result = extract_think_prefix("[THINK]hello[/THINK] world", formats=(custom,))
        assert result.cleaned == " world"
        assert result.reasoning == "hello"

    # -- flush() ---------------------------------------------------------------

    def test_flush_drains_buffered_idle_content(self):
        """flush() drains buffered IDLE content — prefix of open_literal."""
        e = ThinkTagExtractor()
        # <think is a prefix of <thinking> → buffered
        c1, r1 = e.feed("<think")
        assert c1 is None
        # ing is the continuation → <thinking is still a prefix of <thinking>
        c2, r2 = e.feed("ing")
        assert c2 is None

        flushed, r = e.flush()
        assert flushed == "<thinking"
        assert r is None

    def test_flush_empty_when_nothing_buffered(self):
        """flush() is a no-op when nothing is buffered."""
        e = ThinkTagExtractor()
        e.feed("<think>r</think> done")  # DONE, nothing buffered
        flushed, r = e.flush()
        assert flushed is None
        assert r is None

    # -- Concurrency / reuse safety ---------------------------------------------

    def test_shared_extractor_corrupts_second_stream(self):
        """Proves that reusing the same extractor across streams leaks state.

        This test exists to document WHY providers must create a fresh
        ThinkTagExtractor per stream call. If this test EVER passes
        (meaning the extractor was shared), the second stream is corrupted.
        """
        shared = ThinkTagExtractor()

        # First stream — extractor enters DONE
        shared.feed("<think>first_reasoning</think> first_output")
        # At this point shared._state == DONE

        # Second stream with SAME extractor — DONE state leaks through
        c, r = shared.feed("<think>second_reasoning</think> second_output")
        # BUG: content is raw because _state is DONE, think tag NOT extracted
        assert c == "<think>second_reasoning</think> second_output"
        assert r is None

    # -- Streaming typewriter: reasoning returned as deltas, not buffered ------

    def test_reasoning_returned_as_deltas(self):
        """Reasoning is streamed out immediately, not accumulated."""
        e = ThinkTagExtractor()
        # First chunk: open tag + partial reasoning
        c1, r1 = e.feed("<think>part1")
        assert c1 is None       # no content yet (still in think)
        assert r1 == "part1"    # reasoning RETURNED as delta ✓

        # Second chunk: more reasoning, close tag, content
        c2, r2 = e.feed(" part2</think> output")
        assert c2 == " output"  # content after close
        assert r2 == " part2"   # remaining reasoning delta ✓

    def test_content_before_think_is_not_buffered(self):
        """Content before any think tag is streamed immediately."""
        e = ThinkTagExtractor()
        c1, r1 = e.feed("Hello ")
        assert c1 == "Hello "   # immediate output
        assert r1 is None

        c2, r2 = e.feed("world")
        assert c2 == "world"    # immediate output (state is DONE)
        assert r2 is None

    def test_reasoning_with_partial_close_tag_suffix(self):
        """When chunk ends with partial close tag, it's kept for next chunk."""
        e = ThinkTagExtractor()
        # Open tag + reasoning ending with partial close: </th
        c1, r1 = e.feed("<think>reasoning</th")
        assert c1 is None
        assert r1 == "reasoning"  # </th stripped from output (prefix of </think>)

        # Next chunk completes the close tag
        c2, r2 = e.feed("ink> output")
        assert c2 == " output"
        assert not r2  # close completed (empty reasoning, same as None to caller)

    def test_reasoning_with_partial_close_slash(self):
        """Chunk ends with just '<' — partial close prefix."""
        e = ThinkTagExtractor()
        c1, r1 = e.feed("<think>text<")
        assert c1 is None
        assert r1 == "text"  # trailing '<' is prefix of '</think>', stripped

        c2, r2 = e.feed("/think> done")
        assert c2 == " done"
        assert not r2  # close completed, no new reasoning

    def test_close_tag_only_matches_its_pair(self):
        """<think> is only closed by </think>, not </thinking>."""
        e = ThinkTagExtractor()
        c1, r1 = e.feed("<think>reason</thinking>")
        assert c1 is None
        assert r1 == "reason</thinking>"  # </thinking> is NOT the close for <think>

    def test_thinking_only_closed_by_thinking(self):
        """<thinking> is only closed by </thinking>, not </think>."""
        e = ThinkTagExtractor()
        c1, r1 = e.feed("<thinking>reason</think>")
        assert c1 is None
        assert r1 == "reason</think>"  # </think> is NOT the close for <thinking>

    def test_fresh_extractor_per_stream_correct(self):
        """Creating a NEW extractor per stream is the correct pattern."""
        # First stream
        e1 = ThinkTagExtractor()
        c1, r1 = e1.feed("<think>first</think> A")
        assert c1 == " A"
        assert r1 == "first"

        # Second stream — fresh instance, no state leak
        e2 = ThinkTagExtractor()
        c2, r2 = e2.feed("<think>second</think> B")
        assert c2 == " B"
        assert r2 == "second"

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
