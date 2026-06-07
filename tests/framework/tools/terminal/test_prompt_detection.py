"""Test prompt and input detection heuristics."""

from __future__ import annotations

import pytest

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
