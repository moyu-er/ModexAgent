from __future__ import annotations

from framework.tools.terminal.results import SlidingOutputBuffer


def test_append_and_text_returns_content() -> None:
    buf = SlidingOutputBuffer(max_chars=1000, max_commands=10)
    buf.append("hello ")
    buf.append("world")
    assert buf.text == "hello world"


def test_mark_command_boundary_seals_current_parts() -> None:
    buf = SlidingOutputBuffer(max_chars=1000, max_commands=10)
    buf.append("cmd1 output")
    buf.mark_command_boundary()
    buf.append("cmd2 output")
    assert buf.text == "cmd1 outputcmd2 output"


def test_char_constraint_trims_oldest_commands() -> None:
    buf = SlidingOutputBuffer(max_chars=20, max_commands=100)
    buf.append("a" * 10)
    buf.mark_command_boundary()
    buf.append("b" * 10)
    buf.mark_command_boundary()
    buf.append("c" * 10)
    buf.mark_command_boundary()
    assert "a" * 10 not in buf.text
    assert "b" * 10 in buf.text
    assert "c" * 10 in buf.text
    assert buf.total_chars <= 20


def test_command_constraint_limits_deque_size() -> None:
    buf = SlidingOutputBuffer(max_chars=1_000_000, max_commands=3)
    for i in range(5):
        buf.append(f"cmd{i}")
        buf.mark_command_boundary()
    text = buf.text
    assert "cmd0" not in text
    assert "cmd1" not in text
    assert "cmd2" in text
    assert "cmd3" in text
    assert "cmd4" in text


def test_clear_resets_all_state() -> None:
    buf = SlidingOutputBuffer()
    buf.append("data")
    buf.mark_command_boundary()
    buf.append("more")
    buf.clear()
    assert buf.text == ""
    assert buf.total_chars == 0


def test_mark_command_boundary_with_empty_current_parts_is_noop() -> None:
    buf = SlidingOutputBuffer()
    buf.append("data")
    buf.mark_command_boundary()
    buf.mark_command_boundary()
    assert buf.text == "data"


def test_total_chars_property() -> None:
    buf = SlidingOutputBuffer()
    buf.append("hello")
    assert buf.total_chars == 5
    buf.append(" world")
    assert buf.total_chars == 11
