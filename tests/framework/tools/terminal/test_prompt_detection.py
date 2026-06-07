"""Test prompt and input detection heuristics."""

from __future__ import annotations

import pytest

from framework.tools.terminal.backends.base import extract_current_segment_from_buffer
from framework.tools.terminal.prompt import (
    extract_last_command_output,
    is_prompt_ready,
    is_waiting_for_input,
)


class TestIsWaitingForInput:
    def test_password_prompt(self) -> None:
        assert is_waiting_for_input("[sudo] password for user: ") is True

    def test_yn_prompt(self) -> None:
        assert is_waiting_for_input("Do you want to continue? [y/n] ") is True

    def test_login_prompt(self) -> None:
        assert is_waiting_for_input("login: ") is True

    def test_normal_output_not_input_wait(self) -> None:
        assert is_waiting_for_input("Building package (1/100)...") is False

    def test_empty_not_input_wait(self) -> None:
        assert is_waiting_for_input("") is False

    def test_repaint_progress_not_input_wait(self) -> None:
        assert is_waiting_for_input("\rProgress: [##########] 100%") is False

    def test_password_in_middle_not_trigger(self) -> None:
        assert is_waiting_for_input("echo Your password is hunter2") is False

    def test_password_not_on_last_line(self) -> None:
        text = "Some output\npassword: enter\nmore output"
        assert is_waiting_for_input(text) is False


class TestIsPromptReady:
    def test_bash_prompt(self) -> None:
        assert is_prompt_ready("user@host:~$ ") is True

    def test_root_prompt(self) -> None:
        assert is_prompt_ready("root@server:~# ") is True

    def test_powershell_prompt(self) -> None:
        assert is_prompt_ready("PS C:\\Users>") is True

    def test_regular_output_not_prompt(self) -> None:
        assert is_prompt_ready("Hello world") is False

    def test_empty_not_prompt(self) -> None:
        assert is_prompt_ready("") is False


class TestExtractCurrentSegment:
    """Verify extract_current_segment_from_buffer correctness."""

    def test_real_prompt_detected(self) -> None:
        segment = extract_current_segment_from_buffer("$ ")
        assert segment.is_empty_prompt is True
        assert segment.cursor_line == "$ "

    def test_command_output_not_prompt(self) -> None:
        """Plain command output must NOT be an empty prompt."""
        segment = extract_current_segment_from_buffer("Building package...")
        assert segment.is_empty_prompt is False

    def test_command_output_with_path_not_prompt(self) -> None:
        """Path-like output must NOT be an empty prompt."""
        segment = extract_current_segment_from_buffer("Installing to C:\\Program Files\\app")
        assert segment.is_empty_prompt is False

    def test_running_command_no_prompt(self) -> None:
        segment = extract_current_segment_from_buffer("$ npm install\nFetching packages...")
        assert segment.is_empty_prompt is False
        assert "Fetching packages" in segment.text

    def test_completed_command_with_prompt(self) -> None:
        segment = extract_current_segment_from_buffer("$ npm install\nDone\n$ ")
        assert segment.is_empty_prompt is True
        assert segment.cursor_line == "$ "


class TestExtractLastCommandOutput:
    def test_completed_command(self) -> None:
        text = "$ pwd\n/home/user\n$ "
        result = extract_last_command_output(text)
        assert "$ pwd" in result
        assert "/home/user" in result

    def test_running_command(self) -> None:
        text = "$ npm install\nFetching packages...\n"
        result = extract_last_command_output(text)
        assert "$ npm install" in result
        assert "Fetching packages" in result

    def test_idle_prompt(self) -> None:
        text = "$ "
        result = extract_last_command_output(text)
        assert "$" in result.strip()
