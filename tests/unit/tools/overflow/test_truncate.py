from __future__ import annotations

from modex_agent.tools.overflow.truncate import (
    DEFAULT_HEAD_RATIO,
    DEFAULT_TAIL_RATIO,
    render_overflow_text,
    split_head_tail,
)


class TestSplitHeadTail:
    def test_default_ratios_are_threshold_fractions(self) -> None:
        assert DEFAULT_HEAD_RATIO == 0.10
        assert DEFAULT_TAIL_RATIO == 0.15
        assert split_head_tail(50_000) == (5_000, 7_500)
        assert split_head_tail(50) == (5, 7)

    def test_custom_ratios(self) -> None:
        assert split_head_tail(100, head_ratio=0.5, tail_ratio=0.5) == (50, 50)
        assert split_head_tail(100, head_ratio=0.0) == (0, 15)
        assert split_head_tail(100, tail_ratio=0.0) == (10, 0)

    def test_shown_parts_sum_to_quarter_of_threshold(self) -> None:
        # The shown head+tail is a fraction of the THRESHOLD, not the
        # budget: 10% + 15% = 25% shown, 75%+ elided (persisted to disk by
        # the overflow handler when one is installed).
        for max_chars in (1, 7, 51, 999, 50_000):
            head, tail = split_head_tail(max_chars)
            assert head + tail <= int(max_chars * 0.25) + 1


class TestRenderOverflowText:
    def test_shape_head_marker_tail_path(self) -> None:
        content = "H" * 40 + "M" * 250 + "T" * 40
        text = render_overflow_text(
            content,
            head_chars=40,
            tail_chars=40,
            full_output_path="/ovf/s1/c1/full.txt",
        )

        lines = text.split("\n")
        # head / "" / marker / "" / tail / "" / notice
        assert len(lines) == 7
        assert lines[0] == "H" * 40
        assert lines[1] == ""
        assert lines[4] == "T" * 40
        assert lines[2].startswith("[... OUTPUT ELIDED: 250 chars")
        assert "truncation marker, NOT tool output" in lines[2]
        assert lines[6].startswith("[Full output (330 chars total) saved to: /ovf/s1/c1/full.txt")
        assert "read tool" in lines[6]

    def test_elision_counts_chars_and_lines(self) -> None:
        middle = "m\n" * 100  # 200 chars, 100 newlines
        content = "H" * 10 + middle + "T" * 10
        text = render_overflow_text(
            content,
            head_chars=10,
            tail_chars=10,
            full_output_path="/p/full.txt",
        )

        assert "OUTPUT ELIDED: 200 chars" in text
        assert "(~101 lines)" in text

    def test_under_limit_returns_unchanged(self) -> None:
        content = "H" * 30 + "T" * 30
        assert (
            render_overflow_text(
                content, head_chars=40, tail_chars=40, full_output_path="/p/full.txt"
            )
            == content
        )

    def test_exact_budget_returns_unchanged(self) -> None:
        content = "a" * 60
        assert (
            render_overflow_text(
                content, head_chars=30, tail_chars=30, full_output_path="/p/full.txt"
            )
            == content
        )

    def test_tiny_elision_returns_unchanged(self) -> None:
        # omitted (10) is shorter than the elision marker itself — eliding
        # would inflate the text while losing content.
        content = "H" * 30 + "M" * 10 + "T" * 30
        assert (
            render_overflow_text(
                content, head_chars=30, tail_chars=30, full_output_path="/p/full.txt"
            )
            == content
        )

    def test_no_path_variant_claims_not_saved(self) -> None:
        content = "H" * 40 + "M" * 250 + "T" * 40
        text = render_overflow_text(content, head_chars=40, tail_chars=40)

        lines = text.split("\n")
        assert len(lines) == 7
        assert lines[0] == "H" * 40
        assert lines[4] == "T" * 40
        assert "OUTPUT ELIDED: 250 chars" in lines[2]
        assert "NOT saved (overflow handler unavailable)" in lines[2]
        assert lines[6].startswith("[Full output (330 chars total) NOT saved to disk")
        assert "full.txt" not in text
        assert "saved to:" not in text

    def test_zero_tail_does_not_leak_full_content(self) -> None:
        # content[-0:] would slice the WHOLE content — guard the boundary.
        content = "H" * 10 + "M" * 300
        text = render_overflow_text(
            content, head_chars=10, tail_chars=0, full_output_path="/p/full.txt"
        )

        lines = text.split("\n")
        assert len(lines) == 7
        assert lines[0] == "H" * 10
        assert lines[4] == ""

    def test_multibyte_chars_count_as_chars(self) -> None:
        content = "头" * 400
        text = render_overflow_text(
            content, head_chars=100, tail_chars=100, full_output_path="/p/full.txt"
        )

        lines = text.split("\n")
        assert lines[0] == "头" * 100
        assert lines[4] == "头" * 100
        assert "OUTPUT ELIDED: 200 chars" in lines[2]
