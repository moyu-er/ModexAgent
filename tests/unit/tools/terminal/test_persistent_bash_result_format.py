"""Pure result-formatting contract for the persistent bash pair.

``_format_result`` is the single exit for a command's content part and
``_with_hint``/``_with_notice`` the advisory-join seams; these tests pin
the empty-output placeholder and join semantics WITHOUT a PTY — the
real-protocol suites (``test_persistent_bash.py``,
``test_persistent_bash_session.py``) are POSIX-gated, so this file keeps
the contract verified on every platform.

Placeholder boundary (user-facing contract): only a LENGTH-ZERO result
(after the single trailing-newline strip) becomes ``[no output]`` — the
leading/trailing whitespace of real output is meaningful and is
preserved verbatim, never stripped.
"""

from __future__ import annotations

import modex_agent.tools.terminal._persistent_session as session_mod


def test_length_zero_result_becomes_no_output_placeholder():
    """Empty results (raw "" / a bare newline / PTY CRLF) become
    `[no output]` — the content part is never empty."""
    assert session_mod._format_result("", None, None) == "[no output]"
    assert session_mod._format_result("\n", None, None) == "[no output]"
    assert session_mod._format_result("\r\n", None, None) == "[no output]"


def test_whitespace_only_result_preserved_verbatim():
    """Real output whitespace is meaningful — never stripped, never
    replaced by the placeholder."""
    assert session_mod._format_result(" \n", None, None) == " "
    assert session_mod._format_result("  pad  ", None, None) == "  pad  "
    assert session_mod._format_result("\n\n", None, None) == "\n"
    # PS1-token residue leaves its trailing space behind — still content.
    assert session_mod._format_result("__MODEX_PS1__ \n", None, None) == " "


def test_exit_code_marker_always_newline_separated():
    """A failed command's marker joins after a newline — including when
    the body is the `[no output]` placeholder (no marker-alone form)."""
    assert session_mod._format_result("", 1, None) == "[no output]\n[exit code: 1]"
    assert session_mod._format_result("out", 2, None) == "out\n[exit code: 2]"
    assert session_mod._format_result("  x  ", 3, None) == "  x  \n[exit code: 3]"


def test_hint_and_notice_join_after_separator():
    """Hints join after a blank line, notices after one newline — the
    never-empty content part makes the join unconditional."""
    assert session_mod._with_hint("[no output]", "[hint: x]") == "[no output]\n\n[hint: x]"
    assert session_mod._with_notice("[no output]", "[n]") == "[no output]\n[n]"
