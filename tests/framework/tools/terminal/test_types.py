"""Verify terminal type enums and helpers."""

from framework.tools.terminal.types import (
    CommandResultStatus,
    ShellFamily,
    TerminalCommandStatus,
)


class TestTerminalCommandStatus:
    def test_all_expected_values_present(self) -> None:
        expected = {
            "unknown", "idle", "executing", "long_running", "stuck",
            "waiting_input", "paginated", "completed", "timed_out",
        }
        actual = {s.value for s in TerminalCommandStatus}
        assert actual == expected

    def test_long_running_exists(self) -> None:
        assert TerminalCommandStatus.LONG_RUNNING.value == "long_running"


class TestCommandResultStatus:
    def test_rejected_exists(self) -> None:
        assert CommandResultStatus.REJECTED.value == "rejected"

    def test_all_values(self) -> None:
        expected = {
            "completed", "executing", "timed_out", "paginated",
            "waiting_input", "stuck", "rejected",
        }
        actual = {s.value for s in CommandResultStatus}
        assert actual == expected


class TestShellFamily:
    def test_bash_uses_readline(self) -> None:
        assert ShellFamily.BASH.uses_readline() is True

    def test_cmd_not_uses_readline(self) -> None:
        assert ShellFamily.CMD.uses_readline() is False

    def test_bash_command_ending_newline(self) -> None:
        assert ShellFamily.BASH.command_ending() == "\n"

    def test_cmd_command_ending_crlf(self) -> None:
        assert ShellFamily.CMD.command_ending() == "\r\n"
