from __future__ import annotations

from modex_agent.tools.terminal.backends.base import extract_current_segment_from_buffer


def test_extract_current_segment_returns_empty_prompt() -> None:
    segment = extract_current_segment_from_buffer("Microsoft Windows\nC:\\repo>")

    assert segment.text == "C:\\repo>"
    assert segment.is_empty_prompt is True


def test_extract_current_segment_returns_last_command_to_now() -> None:
    text = "C:\\repo>git status\nOn branch main\nC:\\repo>npm"

    segment = extract_current_segment_from_buffer(text)

    assert segment.text == "C:\\repo>npm"
    assert segment.cursor_line == "C:\\repo>npm"


def test_extract_current_segment_handles_unix_prompt() -> None:
    text = "user@host:~$ ls\nfile1.txt\nfile2.txt\nuser@host:~$"

    segment = extract_current_segment_from_buffer(text)

    assert segment.text == "user@host:~$"
    assert segment.is_empty_prompt is True


def test_extract_current_segment_handles_empty_input() -> None:
    segment = extract_current_segment_from_buffer("")

    assert segment.text == ""
    assert segment.is_empty_prompt is True
