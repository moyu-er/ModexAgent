"""Tests for shell detection and ShellInfo."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add project root to path for imports
project_root = Path(__file__).parent.parent.parent.parent.parent
sys.path.insert(0, str(project_root))

from framework.tools.standard.shell_tool import ShellInfo, detect_platform_shell


class TestShellInfo:
    def test_shell_info_creation(self) -> None:
        info = ShellInfo(name="bash", path="/bin/bash", platform="linux", is_stateful=False)
        assert info.name == "bash"
        assert info.path == "/bin/bash"
        assert info.platform == "linux"
        assert info.is_stateful is False

    def test_shell_info_immutable(self) -> None:
        info = ShellInfo(name="bash", path="/bin/bash", platform="linux", is_stateful=False)
        with pytest.raises(AttributeError):
            info.name = "zsh"


class TestDetectPlatformShell:
    def test_detect_bash_on_windows(self) -> None:
        """Windows: bash > powershell > cmd."""
        with (
            patch("shutil.which") as mock_which,
            patch("subprocess.run") as mock_run,
            patch("platform.system", return_value="Windows"),
        ):
            mock_which.side_effect = lambda x: {
                "bash": "C:\\Git\\bin\\bash.exe",
                "powershell": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
                "cmd": "C:\\Windows\\System32\\cmd.exe",
                "cmd.exe": "C:\\Windows\\System32\\cmd.exe",
            }.get(x)
            mock_run.return_value = type("Result", (), {"returncode": 0, "stdout": "GNU bash, version 5.2.0", "stderr": ""})()

            info = detect_platform_shell()
            assert info.name == "bash"
            assert info.platform == "windows"
            assert "bash" in info.path.lower()

    def test_fallback_to_powershell_when_bash_fails(self) -> None:
        with (
            patch("shutil.which") as mock_which,
            patch("subprocess.run") as mock_run,
            patch("platform.system", return_value="Windows"),
        ):
            mock_which.side_effect = lambda x: {
                "bash": "C:\\Git\\bin\\bash.exe",
                "powershell": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
                "pwsh": None,
                "cmd": "C:\\Windows\\System32\\cmd.exe",
                "cmd.exe": "C:\\Windows\\System32\\cmd.exe",
            }.get(x)

            def side_effect(*args, **kwargs):
                if args and "bash" in str(args[0]):
                    return type("Result", (), {"returncode": 1, "stdout": ""})()
                return type("Result", (), {"returncode": 0, "stdout": ""})()

            mock_run.side_effect = side_effect

            info = detect_platform_shell()
            assert info.name == "powershell"

    def test_fallback_to_cmd(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            patch("platform.system", return_value="Windows"),
        ):
            info = detect_platform_shell()
            assert info.name == "cmd"
            assert info.platform == "windows"

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

    def test_env_shell_respected(self) -> None:
        with (
            patch("platform.system", return_value="Linux"),
            patch.dict("os.environ", {"SHELL": "/usr/bin/zsh"}),
            patch("shutil.which", return_value="/usr/bin/zsh"),
        ):
            info = detect_platform_shell()
            assert info.name == "zsh"
            assert info.path == "/usr/bin/zsh"
