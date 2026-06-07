from __future__ import annotations

import pytest

from framework.tools.terminal.types import Platform, ShellFamily, ShellInfo


class TestPlatform:
    """Tests for the Platform enum."""

    def test_members(self) -> None:
        assert Platform.WINDOWS.value == "windows"
        assert Platform.LINUX.value == "linux"
        assert Platform.DARWIN.value == "darwin"

    def test_is_str_enum(self) -> None:
        assert issubclass(Platform, str)


class TestShellFamily:
    """Tests for the ShellFamily enum and its methods."""

    def test_members(self) -> None:
        assert ShellFamily.BASH.value == "bash"
        assert ShellFamily.CMD.value == "cmd"
        assert ShellFamily.ZSH.value == "zsh"
        assert ShellFamily.SH.value == "sh"

    def test_uses_readline(self) -> None:
        assert ShellFamily.BASH.uses_readline() is True
        assert ShellFamily.ZSH.uses_readline() is True
        assert ShellFamily.SH.uses_readline() is True
        assert ShellFamily.CMD.uses_readline() is False

    def test_command_ending(self) -> None:
        assert ShellFamily.BASH.command_ending() == "\n"
        assert ShellFamily.ZSH.command_ending() == "\n"
        assert ShellFamily.SH.command_ending() == "\n"
        assert ShellFamily.CMD.command_ending() == "\r\n"


class TestShellInfo:
    """Tests for the ShellInfo frozen dataclass."""

    def test_creation(self) -> None:
        info = ShellInfo(
            family=ShellFamily.BASH,
            path="/bin/bash",
            platform=Platform.LINUX,
        )
        assert info.family == ShellFamily.BASH
        assert info.path == "/bin/bash"
        assert info.platform == Platform.LINUX

    def test_frozen(self) -> None:
        info = ShellInfo(
            family=ShellFamily.BASH,
            path="/bin/bash",
            platform=Platform.LINUX,
        )
        with pytest.raises(AttributeError):
            info.path = "/usr/bin/bash"

    def test_name_property(self) -> None:
        bash_info = ShellInfo(
            family=ShellFamily.BASH,
            path="/bin/bash",
            platform=Platform.LINUX,
        )
        assert bash_info.name == "bash"

        cmd_info = ShellInfo(
            family=ShellFamily.CMD,
            path="cmd.exe",
            platform=Platform.WINDOWS,
        )
        assert cmd_info.name == "cmd"

    def test_equality(self) -> None:
        info_a = ShellInfo(
            family=ShellFamily.ZSH,
            path="/bin/zsh",
            platform=Platform.DARWIN,
        )
        info_b = ShellInfo(
            family=ShellFamily.ZSH,
            path="/bin/zsh",
            platform=Platform.DARWIN,
        )
        assert info_a == info_b

    def test_inequality(self) -> None:
        info_a = ShellInfo(
            family=ShellFamily.BASH,
            path="/bin/bash",
            platform=Platform.LINUX,
        )
        info_b = ShellInfo(
            family=ShellFamily.ZSH,
            path="/bin/zsh",
            platform=Platform.DARWIN,
        )
        assert info_a != info_b


def test_terminal_command_status_values() -> None:
    from framework.tools.terminal.types import TerminalCommandStatus

    expected = {
        "unknown", "idle", "executing", "waiting_input",
        "stuck", "completed", "timed_out", "paginated",
    }
    actual = {s.value for s in TerminalCommandStatus}
    assert actual == expected


def test_terminal_command_status_is_string() -> None:
    from framework.tools.terminal.types import TerminalCommandStatus

    assert TerminalCommandStatus.EXECUTING == "executing"
    assert isinstance(TerminalCommandStatus.UNKNOWN, str)
