"""One-shot ShellExecutor for resolved LOCAL and OCI launchers.

Implements the ``ShellExecutor`` ABC from ``tools/terminal/subprocess_tool``
(the injection point ``SubprocessTool`` already accepts) for container
execution: each command becomes ``[engine, "exec", [-w, dir,] <container>,
*command_argv]``.

Execution contract:

- The original command is one argument to the target bash's ``-c``.
  Operators and quoting are interpreted there, never by a host shell.
- OCI ``working_dir`` maps to ``exec -w`` so cwd semantics match the
  persistent-shell seam (same-path mount invariant).
- Local launchers inherit the full host environment and runtime overrides.
  OCI CLI processes inherit it too, but container commands use the image's
  environment; this executor does not inject host variables into containers.

The prefix comes from ``ResolvedSandbox.one_shot_command_argv_prefix``.
LOCAL keeps the prefix intact and uses subprocess cwd; OCI inserts exec -w.
Only a confirmed missing launcher before child creation can select HOST via
the shared binding. Nonzero exits, timeouts, and uncertain operations never
authorize replay on the host.
"""

from __future__ import annotations

import asyncio
import errno
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.tools.terminal.subprocess_tool import ShellExecutor, create_subprocess_executor
from modex_agent.tools.terminal.types import (
    Platform,
    ShellFamily,
    ShellInfo,
)

from .guard_presentation import is_container_dead, sandbox_restarted_error_text
from .oci_support import ContainerMount
from .settings import SandboxBackend

if TYPE_CHECKING:
    from .shell_plan import SandboxBinding

if sys.platform == "win32":
    from subprocess import CREATE_NEW_PROCESS_GROUP as _PROCESS_GROUP_CREATION_FLAGS

    _START_NEW_SESSION = False
else:
    _PROCESS_GROUP_CREATION_FLAGS = 0
    _START_NEW_SESSION = True

# Output shaping mirrors SubprocessExecutor (the seam's existing contract):
# stdout + STDERR section + exit-code line.
_NO_OUTPUT = "(no output)"


class ContainerShellExecutor(ShellExecutor):
    """Execute one-shot commands through the selected LOCAL/OCI launcher.

    ``command_prefix`` is the ``one_shot_command_argv_prefix`` from a
    resolved sandbox: a local wrapper or ``[engine, "exec", <container>]``.
    """

    def __init__(
        self, command_prefix: list[str], *, backend: SandboxBackend = SandboxBackend.OCI,
        shell_path: str = "/bin/bash",
        binding: SandboxBinding | None = None,
    ) -> None:
        if not command_prefix:
            raise ValueError("command_prefix must be a non-empty argv prefix")
        self._prefix = list(command_prefix)
        if backend not in (SandboxBackend.LOCAL, SandboxBackend.OCI):
            raise ValueError("sandbox executor requires LOCAL or OCI")
        self._backend = backend
        self._shell_path = shell_path
        self._binding = binding

    async def execute(
        self,
        command: str,
        working_dir: str | None = None,
        timeout: int | None = 300,
    ) -> str:
        from modex_agent.runtime.env_context import _current_session_id, _modex_env
        from modex_agent.tools.terminal.env import build_full_env

        if self._binding is not None and self._binding.current().backend is SandboxBackend.HOST:
            return await create_subprocess_executor().execute(command, working_dir, timeout)

        # exec options must precede the container operand (CLI parsing):
        # engine, exec, [-w, dir], container, command...
        argv = list(self._prefix)
        if self._backend is SandboxBackend.OCI and working_dir is not None:
            sandbox_dir = ContainerMount.for_path(Path(working_dir)).sandbox_path
            argv[2:2] = ["-w", sandbox_dir]
        argv.extend([self._shell_path, "--noprofile", "--norc", "-c", command])

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=_START_NEW_SESSION,
                creationflags=_PROCESS_GROUP_CREATION_FLAGS,
                cwd=working_dir if self._backend is SandboxBackend.LOCAL else None,
                env=build_full_env(overrides=_modex_env.get()),
            )
        except OSError as exc:
            # No child process was created: unlike nonzero exit, this proves
            # the target was not submitted. Invalid cwd is not engine failure.
            if (
                self._binding is None
                or exc.errno != errno.ENOENT
                or (working_dir is not None and not Path(working_dir).is_dir())
            ):
                raise
            if not await self._binding.fallback(_current_session_id.get(), str(exc)):
                raise
            return await create_subprocess_executor().execute(command, working_dir, timeout)
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.CancelledError:
            process.kill()
            raise
        except TimeoutError:
            process.kill()
            return f"Error: Command timed out after {timeout} seconds"

        parts: list[str] = []
        if stdout:
            parts.append(stdout.decode("utf-8", errors="replace"))
        if stderr:
            stderr_text = stderr.decode("utf-8", errors="replace")
            if stderr_text.strip():
                parts.append(f"STDERR:\n{stderr_text}")
        if process.returncode != 0:
            parts.append(f"\nExit code: {process.returncode}")
            if self._backend is SandboxBackend.OCI and is_container_dead(
                stderr.decode("utf-8", errors="replace")
            ):
                parts.append(sandbox_restarted_error_text(stderr.decode("utf-8", errors="replace")))
        return "\n".join(parts) if parts else _NO_OUTPUT

    def shell_info(self) -> ShellInfo:
        """The container runs Linux bash — report that honestly."""
        if self._binding is not None and self._binding.current().backend is SandboxBackend.HOST:
            return create_subprocess_executor().shell_info()
        return ShellInfo(
            family=ShellFamily.BASH,
            path=self._prefix[0],
            platform=Platform.LINUX,
        )
