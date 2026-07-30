"""Tests for SubprocessExecutor — env injection, shell routing, sanitization.

Covers four concerns:
1. ``_modex_env`` contextvar override propagation (env injection hook).
2. Shell-family routing: POSIX shells (bash/zsh/sh) use
   ``create_subprocess_exec`` with ``-c``; Windows shells (cmd) use
   ``create_subprocess_shell`` — see CPython ``_execute_child`` Windows
   branch for why ``shell=True`` + ``executable=bash`` is broken.
3. ANSI/SGR colour-code sanitization (``38;5;226m`` etc.).
4. Platform-aware fallback shell (``_default_fallback_shell``) and
   ``create_subprocess_executor`` factory routing.

Both ``create_subprocess_exec`` and ``create_subprocess_shell`` are
mocked so no real subprocess is spawned.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modex_agent.runtime.env_context import _modex_env
from modex_agent.tools.terminal.env import build_full_env
from modex_agent.tools.terminal.subprocess_tool import (
    CmdSubprocessExecutor,
    PosixSubprocessExecutor,
    PowerShellSubprocessExecutor,
    create_subprocess_executor,
)
from modex_agent.tools.terminal.types import Platform, ShellFamily, ShellInfo


def _make_fake_process(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    """Build a fake asyncio.subprocess.Process-like object."""
    process = MagicMock()
    process.communicate = AsyncMock(return_value=(stdout, stderr))
    process.returncode = returncode
    return process


_BASH = ShellInfo(family=ShellFamily.BASH, path="/bin/bash", platform=Platform.LINUX)
_ZSH = ShellInfo(family=ShellFamily.ZSH, path="/bin/zsh", platform=Platform.DARWIN)
_SH = ShellInfo(family=ShellFamily.SH, path="/bin/sh", platform=Platform.LINUX)
_CMD = ShellInfo(family=ShellFamily.CMD, path="cmd.exe", platform=Platform.WINDOWS)
_PS = ShellInfo(family=ShellFamily.POWERSHELL, path="pwsh", platform=Platform.WINDOWS)


@pytest.fixture
def patched_subprocess() -> Iterator[tuple[AsyncMock, AsyncMock]]:
    """Patch both create_subprocess_exec and create_subprocess_shell.

    Yields ``(exec_mock, shell_mock)`` so tests can assert which path
    was taken and inspect call_args.
    """
    fake_process = _make_fake_process()
    exec_mock = AsyncMock(return_value=fake_process)
    shell_mock = AsyncMock(return_value=fake_process)
    with (
        patch(
            "modex_agent.tools.terminal.subprocess_tool.asyncio.create_subprocess_exec",
            new=exec_mock,
        ),
        patch(
            "modex_agent.tools.terminal.subprocess_tool.asyncio.create_subprocess_shell",
            new=shell_mock,
        ),
    ):
        yield exec_mock, shell_mock


class TestEnvInjectionFromContextVar:
    """Verify SubprocessExecutor.execute() reads env overrides from _modex_env."""

    async def test_no_contextvar_uses_default_env(
        self,
        patched_subprocess: tuple[AsyncMock, AsyncMock],
    ) -> None:
        """Without _modex_env set, env kwarg equals build_full_env() + NO_COLOR."""
        exec_mock, shell_mock = patched_subprocess
        assert _modex_env.get() is None

        executor = create_subprocess_executor(_BASH)
        await executor.execute("echo hi")

        assert exec_mock.await_count == 1
        assert shell_mock.await_count == 0
        env_passed = exec_mock.call_args.kwargs["env"]
        expected = build_full_env()
        expected["NO_COLOR"] = "1"
        assert env_passed == expected
        assert not any(k.startswith("MODEX_") for k in env_passed)

    async def test_contextvar_overrides_injected(
        self,
        patched_subprocess: tuple[AsyncMock, AsyncMock],
    ) -> None:
        """_modex_env overrides appear in the subprocess env verbatim."""
        exec_mock, _ = patched_subprocess
        token = _modex_env.set({"MODEX_TASK_ID": "test-123"})
        try:
            executor = create_subprocess_executor(_BASH)
            await executor.execute("echo hi")

            env_passed = exec_mock.call_args.kwargs["env"]
            assert env_passed["MODEX_TASK_ID"] == "test-123"
        finally:
            _modex_env.reset(token)

        assert _modex_env.get() is None

    async def test_contextvar_none_equals_no_overrides(
        self,
        patched_subprocess: tuple[AsyncMock, AsyncMock],
    ) -> None:
        """Explicitly setting _modex_env to None equals the default."""
        exec_mock, _ = patched_subprocess
        token = _modex_env.set(None)
        try:
            executor = create_subprocess_executor(_BASH)
            await executor.execute("echo hi")

            env_passed = exec_mock.call_args.kwargs["env"]
            expected = build_full_env()
            expected["NO_COLOR"] = "1"
            assert env_passed == expected
            assert not any(k.startswith("MODEX_") for k in env_passed)
        finally:
            _modex_env.reset(token)

        assert _modex_env.get() is None


class TestShellFamilyRouting:
    """Verify POSIX shells use create_subprocess_exec; cmd uses create_subprocess_shell."""

    async def test_bash_uses_exec_with_dash_c(
        self,
        patched_subprocess: tuple[AsyncMock, AsyncMock],
    ) -> None:
        """BASH family → create_subprocess_exec(path, '-c', command)."""
        exec_mock, shell_mock = patched_subprocess
        executor = create_subprocess_executor(_BASH)
        await executor.execute("echo hi && ls")

        assert exec_mock.await_count == 1
        assert shell_mock.await_count == 0
        args = exec_mock.call_args.args
        assert args[0] == "/bin/bash"
        assert args[1] == "-c"
        assert args[2] == "echo hi && ls"

    async def test_zsh_uses_exec_with_dash_c(
        self,
        patched_subprocess: tuple[AsyncMock, AsyncMock],
    ) -> None:
        """ZSH family → create_subprocess_exec(path, '-c', command)."""
        exec_mock, shell_mock = patched_subprocess
        executor = create_subprocess_executor(_ZSH)
        await executor.execute("echo hi")

        assert exec_mock.await_count == 1
        assert shell_mock.await_count == 0
        assert exec_mock.call_args.args[0] == "/bin/zsh"

    async def test_sh_uses_exec_with_dash_c(
        self,
        patched_subprocess: tuple[AsyncMock, AsyncMock],
    ) -> None:
        """SH family → create_subprocess_exec(path, '-c', command)."""
        exec_mock, shell_mock = patched_subprocess
        executor = create_subprocess_executor(_SH)
        await executor.execute("echo hi")

        assert exec_mock.await_count == 1
        assert shell_mock.await_count == 0

    async def test_cmd_uses_shell_true(
        self,
        patched_subprocess: tuple[AsyncMock, AsyncMock],
    ) -> None:
        """CMD family → create_subprocess_shell (no executable= to avoid /c injection)."""
        exec_mock, shell_mock = patched_subprocess
        executor = create_subprocess_executor(_CMD)
        await executor.execute("echo hi")

        assert shell_mock.await_count == 1
        assert exec_mock.await_count == 0
        assert "executable" not in shell_mock.call_args.kwargs

    async def test_powershell_uses_exec_with_no_profile_command(
        self,
        patched_subprocess: tuple[AsyncMock, AsyncMock],
    ) -> None:
        """POWERSHELL family → create_subprocess_exec(path, '-NoProfile', '-Command', command)."""
        exec_mock, shell_mock = patched_subprocess
        executor = create_subprocess_executor(_PS)
        await executor.execute("Write-Output hi")

        assert exec_mock.await_count == 1
        assert shell_mock.await_count == 0
        args = exec_mock.call_args.args
        assert args[0] == "pwsh"
        assert args[1] == "-NoProfile"
        assert args[2] == "-Command"
        assert args[3] == "Write-Output hi"


class TestFactoryRouting:
    """Verify create_subprocess_executor picks the right subclass."""

    def test_bash_returns_posix_executor(self) -> None:
        assert isinstance(create_subprocess_executor(_BASH), PosixSubprocessExecutor)

    def test_zsh_returns_posix_executor(self) -> None:
        assert isinstance(create_subprocess_executor(_ZSH), PosixSubprocessExecutor)

    def test_sh_returns_posix_executor(self) -> None:
        assert isinstance(create_subprocess_executor(_SH), PosixSubprocessExecutor)

    def test_cmd_returns_cmd_executor(self) -> None:
        assert isinstance(create_subprocess_executor(_CMD), CmdSubprocessExecutor)

    def test_powershell_returns_powershell_executor(self) -> None:
        assert isinstance(create_subprocess_executor(_PS), PowerShellSubprocessExecutor)


class TestAnsiSanitization:
    """Verify colour escape codes are stripped from stdout/stderr."""

    async def test_sgr_256_color_stripped_from_stdout(
        self,
        patched_subprocess: tuple[AsyncMock, AsyncMock],
    ) -> None:
        """SGR 256-colour sequences like ``\\x1b[38;5;226m`` are removed."""
        exec_mock, _ = patched_subprocess
        exec_mock.return_value = _make_fake_process(
            stdout=b"\x1b[38;5;226myellow text\x1b[0m\n",
        )
        executor = create_subprocess_executor(_BASH)
        result = await executor.execute("echo test")

        assert "38;5;226m" not in result
        assert "yellow text" in result

    async def test_ansi_csi_stripped_from_stderr(
        self,
        patched_subprocess: tuple[AsyncMock, AsyncMock],
    ) -> None:
        """CSI colour codes on stderr are also sanitized."""
        exec_mock, _ = patched_subprocess
        exec_mock.return_value = _make_fake_process(
            stderr=b"\x1b[31merror\x1b[0m",
        )
        executor = create_subprocess_executor(_BASH)
        result = await executor.execute("bad cmd")

        assert "\x1b[31m" not in result
        assert "error" in result

    async def test_no_color_set_in_env(
        self,
        patched_subprocess: tuple[AsyncMock, AsyncMock],
    ) -> None:
        """NO_COLOR=1 is set so well-behaved CLIs skip colour entirely."""
        exec_mock, _ = patched_subprocess
        executor = create_subprocess_executor(_BASH)
        await executor.execute("echo hi")

        env_passed = exec_mock.call_args.kwargs["env"]
        assert env_passed["NO_COLOR"] == "1"


class TestDefaultFallbackShell:
    """Verify the fallback shell is platform-aware, not Windows-biased."""

    def test_posix_fallback_is_sh(self) -> None:
        """On macOS/Linux the fallback is /bin/sh, never cmd.exe."""
        from modex_agent.tools.terminal.subprocess_tool import _default_fallback_shell

        with patch(
            "modex_agent.tools.terminal.subprocess_tool.platform.system", return_value="Darwin"
        ):
            info = _default_fallback_shell()
        assert info.path == "/bin/sh"
        assert info.family == ShellFamily.SH

    def test_windows_fallback_is_cmd(self) -> None:
        """On Windows the fallback remains cmd.exe."""
        from modex_agent.tools.terminal.subprocess_tool import _default_fallback_shell

        with patch(
            "modex_agent.tools.terminal.subprocess_tool.platform.system", return_value="Windows"
        ):
            info = _default_fallback_shell()
        assert info.path == "cmd.exe"
        assert info.family == ShellFamily.CMD
