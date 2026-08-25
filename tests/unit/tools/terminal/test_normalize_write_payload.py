"""normalize_write_payload — the ProcessTool ``write`` input normalizer.

D4 of the terminal-trio split-brain fix: agents habitually pass
``data="y\n"``; the normalizer makes every input form behave identically
in raw mode (sudo/pager/TUI, where ``\\n`` is ignored) and line-buffered
mode (``read``, where ``\\n`` submits early).
"""

from __future__ import annotations

import pytest

from modex_agent.tools.terminal.pty_keys import normalize_write_payload


@pytest.mark.parametrize(
    ("data", "submit", "expected"),
    [
        # The four canonical forms from the design table.
        ("y\n", True, "y\r"),
        ("y\n", False, "y\r"),
        ("y", True, "y\r"),
        ("y", False, "y"),
        # Multi-newline and CRLF variants all collapse to one submit.
        ("y\n\n", True, "y\r"),
        ("y\n\n", False, "y\r"),
        ("y\r\n", True, "y\r"),
        ("y\r\n", False, "y\r"),
        ("y\n\r", False, "y\r"),
        # Bare newlines alone are pure submit intent.
        ("\n", False, "\r"),
        # Interior newlines are preserved (only trailing ones are stripped).
        ("line1\nline2", False, "line1\nline2"),
        ("line1\nline2\n", True, "line1\nline2\r"),
        # Empty input with submit is a bare Enter; without it, nothing.
        ("", True, "\r"),
        ("", False, ""),
        # Empty input after stripping trailing newlines.
        ("\n\n", False, "\r"),
    ],
)
def test_normalize_write_payload(data: str, submit: bool, expected: str) -> None:
    assert normalize_write_payload(data, submit) == expected
