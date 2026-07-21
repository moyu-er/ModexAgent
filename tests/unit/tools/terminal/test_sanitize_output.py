"""Unit test: sanitize_terminal_output strips the ANSI/OSC sequences emitted by
real WSL bash prompts (coloured prompts, OSC title sequences, DA1 pollution).

This locks in the sanitization contract independently of any PTY backend —
the test feeds the exact byte patterns observed in real captures and asserts
the output is plain readable text.
"""
from __future__ import annotations

from modex_agent.tools.terminal.prompt import sanitize_terminal_output


def test_strips_coloured_prompt_with_osc_title() -> None:
    """Real WSL bash prompt: OSC title + colour-coded ``user:host:path``."""
    raw = (
        "\x1b]0;gyt@XXSDDM: /tmp\x07"
        "\x1b[0;32;92mgyt@XXSDDM\x1b[0m:"
        "\x1b[0;34;94m/tmp\x1b[0m$ "
    )
    cleaned = sanitize_terminal_output(raw)
    assert "\x1b" not in cleaned, f"escape sequences survived:\n{cleaned!r}"
    assert "gyt@XXSDDM" in cleaned
    assert "/tmp" in cleaned
    assert "$" in cleaned


def test_strips_git_bash_ps1_with_bracketed_segments() -> None:
    """Real Git Bash PS1: coloured ``USER@HOST MINGW64 /path (branch)``."""
    raw = (
        "\x1b]0;MINGW64:/f/tool/pythonProject/ModexAgent\x07\r\n"
        "\x1b[32mGYT@XXSDDM \x1b[35mMINGW64 \x1b[33m/f/tool/pythonProject/ModexAgent"
        "\x1b[36m (develop_gyt)\x1b[0m\r\n$ "
    )
    cleaned = sanitize_terminal_output(raw)
    assert "\x1b" not in cleaned
    assert "GYT@XXSDDM" in cleaned
    assert "MINGW64" in cleaned
    assert "(develop_gyt)" in cleaned


def test_strips_decset_cursor_visibility_and_da1() -> None:
    """Bracketed-paste enable, cursor-show/hide, DA1 pollution."""
    raw = "\x1b[?25l\x1b[?2004h\x1b[?1004h\x1b[?9001h\x1b[cCommand output\x1b[?25h"
    cleaned = sanitize_terminal_output(raw)
    assert "\x1b" not in cleaned
    assert cleaned.strip() == "Command output"


def test_normalizes_carriage_return_repaints() -> None:
    """Progress-bar style ``\\r`` repaints collapse to the last painted line."""
    raw = "downloading 10%\rdownloading 50%\rdownloading 100%\ndone\n"
    cleaned = sanitize_terminal_output(raw)
    assert "downloading 10%" not in cleaned
    assert "downloading 50%" not in cleaned
    assert "downloading 100%" in cleaned
    assert cleaned.endswith("done\n")


def test_preserves_real_unicode_and_intentional_newlines() -> None:
    """Sanitization must not eat real text content."""
    raw = "echo 你好\n你好，世界\n"
    cleaned = sanitize_terminal_output(raw)
    assert "你好，世界" in cleaned
    assert "\n" in cleaned
