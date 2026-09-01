"""Single source of truth for model-facing overflow truncation text.

Errors, stack traces, and exit codes cluster at the END of tool output, so
truncation keeps both a head and a (larger) tail, marks the elided middle
explicitly, and points at the persisted full output. Both kept parts are
fractions of the *max_chars* threshold — at the default 50K threshold the
model sees a 5K head + 7.5K tail (≈25%); the elided middle persists to
disk via the overflow handler.
"""

from __future__ import annotations

DEFAULT_HEAD_RATIO: float = 0.10
"""Head share of the overflow threshold."""

DEFAULT_TAIL_RATIO: float = 0.15
"""Tail share of the overflow threshold (larger than the head — errors and
exit codes live at the end of tool output)."""


def split_head_tail(
    max_chars: int,
    head_ratio: float = DEFAULT_HEAD_RATIO,
    tail_ratio: float = DEFAULT_TAIL_RATIO,
) -> tuple[int, int]:
    """Split the shown-text budget for an oversized tool output.

    Both parts are fractions of the *max_chars* THRESHOLD, not of each
    other: ``split_head_tail(50_000)`` → ``(5_000, 7_500)``. The shown
    head+tail is deliberately much smaller than the threshold — content
    large enough to overflow is dominated by its elided middle, which the
    overflow handler persists to disk for the read tool.
    """
    return int(max_chars * head_ratio), int(max_chars * tail_ratio)


def render_overflow_text(
    content: str,
    *,
    head_chars: int,
    tail_chars: int,
    full_output_path: str | None = None,
) -> str:
    """Render the model-facing text for an oversized tool output.

    Shape — head, blank line, explicit elision marker, blank line, tail,
    blank line, full-output notice::

        {content[:head_chars]}

        [... OUTPUT ELIDED: {omitted} chars (~{lines} lines) omitted here — ...]

        {content[-tail_chars:]}

        [Full output ({total} chars total) saved to: {path} — ...]

    The elision marker is unmissable and self-describing: the model must see
    that the middle is elided, not actual tool output. Blank lines separate
    the marker and notice from surrounding content — head/tail cut at arbitrary
    character positions, so without separation the marker reads as a line of
    output (and the tail's first half-word glues onto it).

    When *full_output_path* is None (no overflow handler — full output not
    persisted to disk), the marker and notice say so instead of claiming a
    path.

    Returns *content* unchanged when eliding would not shrink it: either the
    head+tail budget already covers the whole content, or the elided middle
    is shorter than the elision marker itself.
    """
    total = len(content)
    omitted = total - head_chars - tail_chars
    if omitted <= 0:
        return content

    omitted_lines = content[head_chars : total - tail_chars].count("\n") + 1
    if full_output_path is not None:
        marker = (
            f"[... OUTPUT ELIDED: {omitted} chars (~{omitted_lines} lines) omitted here — "
            f"this is a truncation marker, NOT tool output. "
            f"Full output saved; see path below ...]"
        )
        notice = (
            f"[Full output ({total} chars total) saved to: {full_output_path} — "
            f"use the read tool to read the full file if you need the elided middle section]"
        )
    else:
        marker = (
            f"[... OUTPUT ELIDED: {omitted} chars (~{omitted_lines} lines) omitted here — "
            f"this is a truncation marker, NOT tool output. "
            f"Full output NOT saved (overflow handler unavailable) ...]"
        )
        notice = (
            f"[Full output ({total} chars total) NOT saved to disk — "
            f"only the head and tail above remain available]"
        )
    if omitted < len(marker):
        return content

    return f"{content[:head_chars]}\n\n{marker}\n\n{content[total - tail_chars :]}\n\n{notice}"
