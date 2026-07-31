"""Unit tests for the OS-layer primitives.

Three responsibilities under test:

- ``ResolvedExecutable`` — frozen Pydantic ``(argv0, extra_args)`` pair.
- ``resolve_executable(name, logger)`` — POSIX no-op; on Windows walks
  a ``.cmd`` shim to the native binary + its pre-args so the spawn
  bypasses the shim's argv handling. Falls back to direct spawn on
  parse failure.
- ``spawn_process_group(args, cwd, env, stdin)`` — spawns a subprocess
  in its own process group so cancellation cascades to children
  (``start_new_session=True`` on POSIX; ``CREATE_NEW_PROCESS_GROUP``
  on Windows).
- ``terminate_process_group(proc)`` — graceful SIGTERM → SIGKILL on
  POSIX; ``taskkill /T /PID`` on Windows. Already-dead processes are
  a no-op.

Platform-conditional branches use ``@pytest.mark.skipif`` so each
test class only runs on the branch it asserts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.agents.external.os_layer import (
    ResolvedExecutable,
    resolve_executable,
    spawn_process_group,
    terminate_process_group,
)

_IS_WINDOWS: bool = sys.platform == "win32"
_IS_POSIX: bool = not _IS_WINDOWS


# ---------------------------------------------------------------------------
# ResolvedExecutable — pure data model
# ---------------------------------------------------------------------------


class TestResolvedExecutable:
    """The frozen (argv0, extra_args) pair."""

    def test_minimal_construction(self) -> None:
        r = ResolvedExecutable(argv0="pi")
        assert r.argv0 == "pi"
        assert r.extra_args == ()

    def test_with_extra_args(self) -> None:
        r = ResolvedExecutable(
            argv0="powershell",
            extra_args=("-File", "C:\\path\\pi.ps1"),
        )
        assert r.argv0 == "powershell"
        assert r.extra_args == ("-File", "C:\\path\\pi.ps1")

    def test_extra_args_is_tuple(self) -> None:
        # ``tuple`` is required by the spec so the frozen model is hashable.
        r = ResolvedExecutable(argv0="node", extra_args=("a.js",))
        assert isinstance(r.extra_args, tuple)

    def test_frozen_rejects_mutation(self) -> None:
        r = ResolvedExecutable(argv0="x")
        with pytest.raises(ValidationError):
            r.argv0 = "y"

    def test_frozen_rejects_mutation_of_extra_args(self) -> None:
        r = ResolvedExecutable(argv0="x", extra_args=("a",))
        with pytest.raises(ValidationError):
            r.extra_args = ("b",)

    def test_hashable(self) -> None:
        # Frozen + tuple extras = equals-equivalent objects share hash.
        a = ResolvedExecutable(argv0="x", extra_args=("a", "b"))
        b = ResolvedExecutable(argv0="x", extra_args=("a", "b"))
        c = ResolvedExecutable(argv0="y", extra_args=("a", "b"))
        assert hash(a) == hash(b)
        assert hash(a) != hash(c)
        assert {a, b} == {a}

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ResolvedExecutable(argv0="x", unknown_field="bad")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# resolve_executable — POSIX branch (current Linux/macOS)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_IS_WINDOWS, reason="POSIX branch — no .cmd walking on Windows")
class TestResolveExecutablePosix:
    """``resolve_executable`` is a no-op on POSIX (no .cmd shims)."""

    def test_passthrough_on_posix(self) -> None:
        r = resolve_executable("pi")
        assert r.argv0 == "pi"
        assert r.extra_args == ()

    def test_passthrough_with_logger(self) -> None:
        r = resolve_executable("modexbot", logger=logging.getLogger("test"))
        assert r.argv0 == "modexbot"
        assert r.extra_args == ()

    def test_passthrough_full_path(self) -> None:
        r = resolve_executable("/usr/bin/pi")
        assert r.argv0 == "/usr/bin/pi"
        assert r.extra_args == ()


# ---------------------------------------------------------------------------
# resolve_executable — Windows branch
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _IS_WINDOWS, reason="Windows-only .cmd shim branch")
class TestResolveExecutableWindows:
    """Windows .cmd / .exe resolution."""

    def test_non_cmd_extension_returns_as_is(self, tmp_path: Path) -> None:
        exe = tmp_path / "mybin.exe"
        exe.write_bytes(b"MZ")  # valid PE header bytes; we never run it.
        r = resolve_executable(str(exe))
        assert r.argv0 == str(exe)
        assert r.extra_args == ()

    def test_cmd_only_on_path_routes_through_cmd_exe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shim = tmp_path / "mytool.cmd"
        shim.write_text("@echo off\r\necho hello %*\r\n")
        monkeypatch.setenv("PATH", str(tmp_path))
        r = resolve_executable("mytool")
        assert r.argv0 == "cmd.exe"
        assert r.extra_args == ("/c", "mytool")

    def test_exe_takes_precedence_over_cmd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "mytool.exe").write_bytes(b"MZ")
        (tmp_path / "mytool.cmd").write_text("@echo off\r\necho hi\r\n")
        monkeypatch.setenv("PATH", str(tmp_path))
        r = resolve_executable("mytool")
        assert r.argv0 == "mytool"
        assert r.extra_args == ()

    def test_neither_exe_nor_cmd_returns_name_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", str(tmp_path))
        r = resolve_executable("missing-tool")
        assert r.argv0 == "missing-tool"
        assert r.extra_args == ()

    def test_non_cmd_file_passes_through_on_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", str(tmp_path))
        exe = tmp_path / "mybin.exe"
        exe.write_bytes(b"MZ")
        r = resolve_executable("mybin")
        assert r.argv0 == "mybin"

    def test_logger_is_optional(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        shim = tmp_path / "broken2.cmd"
        shim.write_text("REM nothing\n")
        monkeypatch.setenv("PATH", str(tmp_path))
        r = resolve_executable("broken2", logger=None)
        assert r.argv0 == "cmd.exe"

    def test_env_var_override_takes_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "mytool.exe").write_bytes(b"MZ")
        (tmp_path / "mytool.cmd").write_text("@echo off\r\necho hi\r\n")
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.setenv("MODEX_MYTOOL_EXECUTABLE", "/custom/path/mytool")
        r = resolve_executable("mytool")
        assert r.argv0 == "/custom/path/mytool"
        assert r.extra_args == ()

    def test_shell_powershell(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        shim = tmp_path / "mytool.cmd"
        shim.write_text("@echo off\r\necho hi\r\n")
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.setenv("MODEX_EXTERNAL_SHELL", "powershell")
        r = resolve_executable("mytool")
        assert r.argv0 == "powershell.exe"
        assert "-Command" in r.extra_args
        assert "mytool" in r.extra_args

    def test_shell_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        shim = tmp_path / "mytool.cmd"
        shim.write_text("@echo off\r\necho hi\r\n")
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.setenv("MODEX_EXTERNAL_SHELL", "none")
        r = resolve_executable("mytool")
        assert r.argv0 == "mytool"
        assert r.extra_args == ()


# ---------------------------------------------------------------------------
# spawn_process_group
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_IS_WINDOWS, reason="POSIX start_new_session branch")
class TestSpawnProcessGroupPosix:
    """POSIX: child lands in a new session, distinct process group."""

    @pytest.mark.asyncio
    async def test_child_process_group_differs_from_parent(self, tmp_path: Path) -> None:
        proc = await spawn_process_group(
            args=[
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
            ],
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
            stdin=None,
        )
        try:
            assert proc.pid is not None
            assert proc.returncode is None
            # POSIX contract: child's pgid is distinct from the parent's.
            assert os.getpgid(proc.pid) != os.getpgid(os.getpid())  # type: ignore[attr-defined]
        finally:
            await terminate_process_group(proc)

    @pytest.mark.asyncio
    async def test_pipes_stdin_stdout_stderr(self, tmp_path: Path) -> None:
        proc = await spawn_process_group(
            args=[
                sys.executable,
                "-c",
                "print('out'); import sys; sys.stderr.write('err\\n'); sys.exit(0)",
            ],
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
            stdin=None,
        )
        stdout, stderr = await proc.communicate()
        assert stdout.strip() == b"out"
        assert stderr.strip() == b"err"
        assert proc.returncode == 0


@pytest.mark.skipif(not _IS_WINDOWS, reason="Windows CREATE_NEW_PROCESS_GROUP branch")
class TestSpawnProcessGroupWindows:
    """Windows: CREATE_NEW_PROCESS_GROUP flag honoured (no portable pgid check)."""

    @pytest.mark.asyncio
    async def test_windows_spawn_succeeds(self, tmp_path: Path) -> None:
        proc = await spawn_process_group(
            args=[
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
            ],
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
            stdin=None,
        )
        try:
            assert proc.pid is not None
            assert proc.returncode is None
        finally:
            await terminate_process_group(proc)

    @pytest.mark.asyncio
    async def test_windows_pipes_stdout_and_stderr(self, tmp_path: Path) -> None:
        proc = await spawn_process_group(
            args=[
                sys.executable,
                "-c",
                "print('out'); import sys; sys.stderr.write('err\\n'); sys.exit(0)",
            ],
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
            stdin=None,
        )
        stdout, stderr = await proc.communicate()
        assert stdout.strip() == b"out"
        assert stderr.strip() == b"err"
        assert proc.returncode == 0


# ---------------------------------------------------------------------------
# terminate_process_group — already-dead case is always-on
# ---------------------------------------------------------------------------


class TestTerminateProcessGroupAlreadyDead:
    """Already-reaped processes are a no-op (not an error)."""

    @pytest.mark.asyncio
    async def test_finished_subprocess_is_silently_handled(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "pass",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        assert proc.returncode is not None
        # Must not raise despite the process being done.
        await terminate_process_group(proc)


# ---------------------------------------------------------------------------
# terminate_process_group — POSIX SIGTERM → SIGKILL on tree
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_IS_WINDOWS, reason="POSIX SIGTERM→SIGKILL branch")
class TestTerminateProcessGroupPosixTree:
    """A grandchild forked by the child is killed when the parent is killed."""

    @pytest.mark.asyncio
    async def test_kills_grandchild_tree(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "grandchild.pid"
        script = tmp_path / "spawn_grandchild.py"
        # Parent forks a child (which writes its PID and sleeps); both
        # sleep so we have a chance to test the kill cascade.
        script.write_text(
            "import os, sys, time\n"
            f"pid = os.fork()\n"
            "if pid == 0:\n"
            f"  open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "  time.sleep(120)\n"
            "else:\n"
            "  time.sleep(120)\n"
        )
        proc = await spawn_process_group(
            args=[sys.executable, str(script)],
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
            stdin=None,
        )
        try:
            # Wait for the grandchild to fork and write its PID.
            for _ in range(100):
                await asyncio.sleep(0.1)
                if pid_file.exists():
                    break
            else:
                pytest.fail("Grandchild never wrote its PID file.")
            grandchild_pid = int(pid_file.read_text().strip())
            # Sanity: the grandchild exists right now.
            os.kill(grandchild_pid, 0)
        except AssertionError:
            await terminate_process_group(proc)
            raise

        # Tear down the whole group. SIGTERM first, then SIGKILL.
        await terminate_process_group(proc)
        # Generous settle window for the SIGKILL to cascade.
        await asyncio.sleep(0.5)

        # The grandchild must be gone.
        with pytest.raises(ProcessLookupError):
            os.kill(grandchild_pid, 0)


# ---------------------------------------------------------------------------
# terminate_process_group — Windows taskkill /T cascade
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _IS_WINDOWS, reason="Windows taskkill /T branch")
class TestTerminateProcessGroupWindowsTree:
    """Taskkill /T kills the whole tree even when grandchildren exist."""

    @pytest.mark.asyncio
    async def test_taskkill_kills_grandchild_tree(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "grandchild.pid"
        child_script = tmp_path / "child_sleep.py"
        child_script.write_text(
            "import os, time\n"
            f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(120)\n"
        )
        parent_script = tmp_path / "parent_spawn.py"
        parent_script.write_text(
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, {str(child_script)!r}])\n"
            "time.sleep(120)\n"
        )

        proc = await spawn_process_group(
            args=[sys.executable, str(parent_script)],
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
            stdin=None,
        )
        try:
            for _ in range(100):
                await asyncio.sleep(0.1)
                if pid_file.exists():
                    break
            else:
                pytest.fail("Grandchild never wrote its PID file.")
            grandchild_pid = int(pid_file.read_text().strip())
        except AssertionError:
            await terminate_process_group(proc)
            raise

        await terminate_process_group(proc)
        await asyncio.sleep(1.0)

        # Use tasklist to confirm the grandchild PID is gone.
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {grandchild_pid}"],
            capture_output=True,
            text=True,
            check=False,
        )
        # tasklist reports "INFO: No tasks are running..." for missing PIDs.
        assert "INFO: No tasks" in result.stdout or str(grandchild_pid) not in result.stdout, (
            f"Grandchild {grandchild_pid} still alive:\n{result.stdout}"
        )


# ---------------------------------------------------------------------------
# sanity: ensure the running test actually crosses the expected branch
# ---------------------------------------------------------------------------


def test_branch_marker() -> None:
    """Spotlight the running branch — useful when CI jumps platforms."""
    if _IS_WINDOWS:
        assert _IS_POSIX is False
    else:
        assert _IS_WINDOWS is False
