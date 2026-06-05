from __future__ import annotations

from framework.tools.terminal.prompt import detect_pager_entry, resolve_cursor_line
from framework.tools.terminal.results import TerminalSegment


def test_detect_pager_entry_bare_colon() -> None:
    assert detect_pager_entry(":") is True


def test_detect_pager_entry_colon_with_spaces() -> None:
    assert detect_pager_entry("  :  ") is True


def test_detect_pager_entry_config_colon_is_not_pager() -> None:
    assert detect_pager_entry("config:") is False


def test_detect_pager_entry_error_colon_is_not_pager() -> None:
    assert detect_pager_entry("error: something failed") is False


def test_detect_pager_entry_empty_string() -> None:
    assert detect_pager_entry("") is False


def test_detect_pager_entry_prompt_is_not_pager() -> None:
    assert detect_pager_entry("user@host:~$ ") is False


def test_resolve_cursor_line_uses_cursor_line_when_present() -> None:
    seg = TerminalSegment(text="line1\nline2", cursor_line="line2", is_empty_prompt=False)
    assert resolve_cursor_line(seg) == "line2"


def test_resolve_cursor_line_falls_back_to_last_nonempty_line() -> None:
    seg = TerminalSegment(text="line1\nline2\n", cursor_line="", is_empty_prompt=False)
    assert resolve_cursor_line(seg) == "line2"


def test_resolve_cursor_line_empty_segment() -> None:
    seg = TerminalSegment(text="", cursor_line="", is_empty_prompt=True)
    assert resolve_cursor_line(seg) == ""


# ---------------------------------------------------------------------------
# Input-prompt detection (extracted from session.py)
# ---------------------------------------------------------------------------

from framework.tools.terminal.prompt import is_waiting_for_input, INPUT_PROMPT_MARKERS


def test_input_prompt_markers_is_public_tuple() -> None:
    assert isinstance(INPUT_PROMPT_MARKERS, tuple)
    assert len(INPUT_PROMPT_MARKERS) > 0
    assert "password" in INPUT_PROMPT_MARKERS
    assert "[y/n]" in INPUT_PROMPT_MARKERS


def test_is_waiting_for_input_password() -> None:
    assert is_waiting_for_input("Enter password: ") is True


def test_is_waiting_for_input_yes_no() -> None:
    assert is_waiting_for_input("Continue? [y/n] ") is True


def test_is_waiting_for_input_normal_output() -> None:
    assert is_waiting_for_input("Build complete. 42 files compiled.") is False


def test_is_waiting_for_input_empty() -> None:
    assert is_waiting_for_input("") is False


def test_is_waiting_for_input_with_ansi_codes() -> None:
    assert is_waiting_for_input("\x1b[32mPassword:\x1b[0m ") is True


def test_is_waiting_for_input_case_insensitive() -> None:
    assert is_waiting_for_input("PASSWORD: ") is True