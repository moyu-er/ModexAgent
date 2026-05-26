"""Tests for shell detection and ShellInfo."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add project root to path for imports
project_root = Path(__file__).parent.parent.parent.parent.parent
sys.path.insert(0, str(project_root))

from framework.tools.terminal.types import Platform, ShellFamily, ShellInfo, detect_platform_shell


class TestShellInfo:
    def test_shell_info_creation(self) -> None:
        info = ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX)
        assert info.name == "bash"
        assert info.path == "/bin/bash"
        assert info.platform == "linux"

    def test_shell_info_immutable(self) -> None:
        info = ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX)
        with pytest.raises(AttributeError):
            info.family = ShellFamily.ZSH


class TestDetectPlatformShell:
    def test_wsl_bash_priority_1(self) -> None:
        """WSL bash (from System32) is priority 1."""
        with (
            patch("shutil.which") as mock_which,
            patch("subprocess.run") as mock_run,
            patch("platform.system", return_value="Windows"),
            patch.object(Path, "is_file", return_value=True),
        ):
            mock_which.return_value = None  # Git bash not in PATH
            mock_run.return_value = type(
                "Result", (), {"returncode": 0, "stdout": "GNU bash, version 5.2.0", "stderr": ""}
            )()

            info = detect_platform_shell()
            assert info is not None
            assert info.family == ShellFamily.BASH
            assert info.platform == Platform.WINDOWS
            assert info.path.endswith("bash.exe")

    def test_git_bash_priority_2(self) -> None:
        """Git bash is priority 2 when WSL bash is absent."""
        with (
            patch("shutil.which") as mock_which,
            patch("subprocess.run") as mock_run,
            patch("platform.system", return_value="Windows"),
            patch.object(Path, "is_file", return_value=False),  # WSL bash not found
        ):
            mock_which.side_effect = lambda x: {
                "bash": r"D:\git\usr\bin\bash.EXE",
            }.get(x)
            mock_run.return_value = type(
                "Result", (), {"returncode": 0, "stdout": "GNU bash, version 5.2.0", "stderr": ""}
            )()

            info = detect_platform_shell()
            assert info is not None
            assert info.family == ShellFamily.BASH
            assert info.platform == Platform.WINDOWS
            assert "git" in info.path.lower()

    def test_powershell_priority_3(self) -> None:
        """PowerShell is priority 3 when no bash is available."""
        with (
            patch("shutil.which") as mock_which,
            patch("platform.system", return_value="Windows"),
            patch.object(Path, "is_file", return_value=False),  # WSL bash not found
        ):
            mock_which.side_effect = lambda x: {
                "bash": None,
                "powershell.exe": r"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe",
            }.get(x)

            info = detect_platform_shell()
            assert info is not None
            assert info.family == ShellFamily.POWERSHELL
            assert info.platform == Platform.WINDOWS
            assert "powershell" in info.path.lower()

    def test_detect_bash_on_linux(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch.dict("os.environ", {}, clear=True),
            patch("shutil.which") as mock_which,
        ):
            mock_which.side_effect = lambda x: {
                "bash": "/usr/bin/bash",
                "sh": "/bin/sh",
            }.get(x)

            info = detect_platform_shell()
            assert info.name == "bash"
            assert info.platform == "linux"
            assert isinstance(info.family, ShellFamily)
            assert isinstance(info.platform, Platform)

    def test_detect_zsh_on_macos(self) -> None:
        with (
            patch("platform.system", return_value="Darwin"),
            patch.dict("os.environ", {}, clear=True),
            patch("shutil.which") as mock_which,
        ):
            mock_which.side_effect = lambda x: {
                "bash": None,
                "zsh": "/bin/zsh",
                "sh": "/bin/sh",
            }.get(x)

            info = detect_platform_shell()
            assert info.name == "zsh"
            assert info.platform == "darwin"
            assert isinstance(info.family, ShellFamily)
            assert isinstance(info.platform, Platform)

    def test_env_shell_respected(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch.dict("os.environ", {"SHELL": "/usr/bin/zsh"}),
            patch("shutil.which", return_value="/usr/bin/zsh"),
        ):
            info = detect_platform_shell()
            assert info.name == "zsh"
            assert info.path == "/usr/bin/zsh"
            assert isinstance(info.family, ShellFamily)
            assert isinstance(info.platform, Platform)

    def test_detect_platform_shell_returns_typed_enums(self) -> None:
        """Verify detect_platform_shell returns properly typed enum values."""
        with (
            patch("platform.system", return_value="Linux"),
            patch.dict("os.environ", {}, clear=True),
            patch("shutil.which") as mock_which,
        ):
            mock_which.side_effect = lambda x: {
                "bash": "/usr/bin/bash",
                "sh": "/bin/sh",
            }.get(x)

            info = detect_platform_shell()
            assert info is not None
            assert isinstance(info.family, ShellFamily)
            assert isinstance(info.platform, Platform)
            assert info.family == ShellFamily.BASH
            assert info.platform == Platform.LINUX
