"""Unit tests for the two-layer input-prompt detector (``is_waiting_for_input``).

Layer 2 (prompt-shaped suffix) is the layer whose absence left non-keyword
prompts (``printf "name: "; read val``) with ZERO evidence on probe-less
hosts (macOS): the poll loop then rode to the command deadline and closed
the tab.  These tests pin both layers of the documented contract.
"""

from __future__ import annotations

from modex_agent.tools.terminal.prompt import is_waiting_for_input

# ── Layer 2 positives: prompt-shaped suffix without any keyword ──


def test_suffix_colon_prompts_detected() -> None:
    assert is_waiting_for_input("name: ") is True
    assert is_waiting_for_input("Name:") is True
    assert is_waiting_for_input("x:") is True
    assert is_waiting_for_input("Choose [1]:") is True


def test_suffix_question_and_bracket_prompts_detected() -> None:
    assert is_waiting_for_input("Ready?") is True
    assert is_waiting_for_input("Enter choice)") is True
    assert is_waiting_for_input("[A]llow, [D]eny, [C]ancel)") is True


def test_ansi_wrapped_prompt_detected_after_stripping() -> None:
    assert is_waiting_for_input("\x1b[32mname: \x1b[0m") is True
    assert is_waiting_for_input("\x1b[01;36mChoose [1]:\x1b[00m ") is True


def test_carriage_repaint_line_detected_via_last_segment() -> None:
    """Progress-bar repaints keep only the most-recently-painted segment —
    the trailing prompt must survive the repaint noise."""
    assert is_waiting_for_input("working 50%\rname: ") is True


def test_only_last_line_considered() -> None:
    """A prompt-shaped EARLIER line with plain output after it is not a wait."""
    assert is_waiting_for_input("Warning: deprecated\nhello world") is False
    assert is_waiting_for_input("Continue?\ndone") is False


# ── Layer 2 negatives: shell prompts and bare remote forms ──


def test_shell_prompts_not_detected() -> None:
    assert is_waiting_for_input("root@host:~#") is False
    assert is_waiting_for_input("root@host:~# ") is False
    assert is_waiting_for_input("$ ") is False
    assert is_waiting_for_input("bash-5.2$ ") is False
    assert is_waiting_for_input("user@host:~ $ ") is False


def test_bare_remote_prompt_shape_not_detected() -> None:
    """The bare ``user@host...:`` remote-shell form ends in ``:`` but is a
    shell prompt, not an input prompt."""
    assert is_waiting_for_input("user@host:") is False
    assert is_waiting_for_input("root@host:~:") is False


def test_at_sign_inside_real_prompt_still_detected() -> None:
    """A line ending in ``:`` that merely CONTAINS ``@`` is still very
    likely a real input prompt — only the bare single-token remote form is
    excluded."""
    assert is_waiting_for_input("user@host's password: ") is True


# ── Layer 1 interaction: ambiguous markers still need punctuation ──


def test_ambiguous_marker_without_suffix_not_detected() -> None:
    assert is_waiting_for_input("Hashing password: 50%") is False
    assert is_waiting_for_input("Confirming transaction...") is False


def test_plain_output_not_detected() -> None:
    assert is_waiting_for_input("") is False
    assert is_waiting_for_input("hello world") is False
    assert is_waiting_for_input("progress 42%") is False


def test_keyword_prompt_still_detected() -> None:
    assert is_waiting_for_input("Enter password: ") is True
    assert is_waiting_for_input("Proceed? [y/n]") is True
