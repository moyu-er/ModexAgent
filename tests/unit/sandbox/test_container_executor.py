"""Tests for ContainerShellExecutor — the oci-family ShellExecutor (Ticket 07).

``ContainerShellExecutor`` implements the ``ShellExecutor`` ABC
(``tools/terminal/subprocess_tool.py``) used by ``SubprocessTool``: it holds
the one-shot argv prefix (``[engine, "exec", <container>]``) resolved by
``OciContainerRuntime`` and prepends it to the command argv.

Contract (PRD §执行面接入):

- argv-array execution ONLY — no ``sh -c '<joined>'`` anywhere
- ``working_dir`` maps to ``docker exec -w`` (cwd semantics preserved)
- shell_info describes the container bash

The spawn seam (``asyncio.create_subprocess_exec``) is patched; these tests
run on every platform and never touch a real engine.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from modex_agent.sandbox.container_executor import ContainerShellExecutor
from modex_agent.tools.terminal.types import Platform, ShellFamily

_PREFIX = ["docker", "exec", "modex-sbx-test"]


class _FakeProcess:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code

    async def communicate(self) -> tuple[bytes, bytes]:
        return (b"hello from container\n", b"")

    def kill(self) -> None:  # pragma: no cover — timeout path not under test
        pass

    @property
    def returncode(self) -> int:
        return self.exit_code


class TestContainerShellExecutor:
    async def test_execute_prepends_prefix_argv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return _FakeProcess()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        executor = ContainerShellExecutor(command_prefix=list(_PREFIX))
        result = await executor.execute("python3 -c 'print(1)'")

        assert seen["argv"] == (*_PREFIX, "/bin/bash", "--noprofile", "--norc", "-c", "python3 -c 'print(1)'")
        assert "hello from container" in result

    async def test_working_dir_maps_to_exec_w(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return _FakeProcess()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        executor = ContainerShellExecutor(command_prefix=list(_PREFIX))
        await executor.execute("ls", working_dir="/ws/project/sub")

        argv = seen["argv"]
        # exec options precede the container operand: engine, exec, -w, dir, ctr, cmd
        assert argv[0:2] == ("docker", "exec")
        assert argv[2] == "-w"
        assert argv[3] == "/ws/project/sub"
        assert argv[4] == "modex-sbx-test"
        assert argv[5:] == ("/bin/bash", "--noprofile", "--norc", "-c", "ls")

    async def test_shell_source_is_one_unchanged_argument(self) -> None:
        """Only the target bash interprets source, never the host shell."""
        seen: dict[str, Any] = {}

        async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
            seen["argv"] = argv
            return _FakeProcess()

        import modex_agent.sandbox.container_executor as ce_mod

        original = ce_mod.asyncio.create_subprocess_exec
        ce_mod.asyncio.create_subprocess_exec = fake_exec
        try:
            executor = ContainerShellExecutor(command_prefix=list(_PREFIX))
            await executor.execute("printf '%s' 'a b' | cat > result && cat result")
        finally:
            ce_mod.asyncio.create_subprocess_exec = original

        argv = seen["argv"]
        assert argv[-2:] == ("-c", "printf '%s' 'a b' | cat > result && cat result")

    async def test_shell_info_reports_container_bash(self) -> None:
        executor = ContainerShellExecutor(command_prefix=list(_PREFIX))
        info = executor.shell_info()
        assert info.family is ShellFamily.BASH
        assert info.platform is Platform.LINUX
        assert "docker" in info.path

    async def test_nonzero_exit_code_in_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
            return _FakeProcess(exit_code=2)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        executor = ContainerShellExecutor(command_prefix=list(_PREFIX))
        result = await executor.execute("false")
        assert "Exit code: 2" in result

    async def test_empty_prefix_rejected(self) -> None:
        with pytest.raises(ValueError, match="command_prefix"):
            ContainerShellExecutor(command_prefix=[])
