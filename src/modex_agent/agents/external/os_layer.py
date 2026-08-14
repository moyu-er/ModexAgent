"""OS-layer primitives — every ``sys.platform`` branch lives here.

Three responsibilities keep provider backends OS-agnostic:

- ``resolve_executable(name, logger)`` — on Windows, decides whether to
  invoke the provider through ``cmd.exe /c`` (when only a ``.cmd`` shim
  exists on PATH) or directly (when a native ``.exe`` is available). On
  POSIX, a no-op pass-through.
- ``spawn_process_group(args, cwd, env, stdin)`` — starts the child in
  its own process group so cancellation reaches the provider's whole
  subprocess tree (``start_new_session=True`` on POSIX;
  ``creationflags=subprocess.CREATE_NEW_PROCESS_GROUP`` on Windows).
- ``terminate_process_group(proc)`` — graceful SIGTERM → SIGKILL on
  POSIX; ``taskkill /T /PID`` on Windows (the ``/T`` flag kills the
  tree). Already-dead processes are a no-op.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

_IS_WINDOWS: Final[bool] = sys.platform == "win32"
_IS_POSIX: Final[bool] = sys.platform != "win32"


# ---------------------------------------------------------------------------
# Resolved executable value object (Pydantic, frozen, hashable)
# ---------------------------------------------------------------------------


class ResolvedExecutable(BaseModel):
    """The ``(argv0, extra_args)`` pair the spawn should invoke.

    Frozen per the framework's type-safety rules. ``extra_args`` is a
    ``tuple`` (not ``list``) so the model is hashable and drop-in usable
    as a ``dict`` key or ``set`` member.

    Attributes:
        argv0: The binary the spawn invokes. On POSIX this is ``name``
            verbatim. On Windows this is either the provider ``.exe``
            name (when one exists on PATH) or ``cmd.exe`` (when only a
            ``.cmd`` shim is available — the shim is passed via
            ``extra_args`` so cmd.exe expands its batch variables).
        extra_args: Pre-arguments prepended to every call. Empty tuple
            for the direct-``.exe`` path; ``("/c", "<name>")`` for the
            cmd.exe-via-shim path.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    argv0: str
    extra_args: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# resolve_executable
# ---------------------------------------------------------------------------


def resolve_executable(
    name: str,
    logger: logging.Logger | None = None,
) -> ResolvedExecutable:
    """Resolve ``name`` to :class:`ResolvedExecutable`.

    Resolution priority (highest first):

    1. **Explicit override** — ``MODEX_<NAME>_EXECUTABLE`` environment
      variable (e.g. ``MODEX_PI_EXECUTABLE=C:\\bin\\pi.exe``). Skips all
      PATH lookup. Useful when the provider is installed in a non-PATH
      location or when the user wants to pin a specific binary.

    2. **Native .exe on PATH** (Windows) — ``<name>.exe`` found via
      ``_find_on_path``. Returns ``ResolvedExecutable(argv0=name)`` so
      ``CreateProcess`` does its own PATH+PATHEXT lookup at spawn time.

    3. **.cmd shim via shell** (Windows) — only ``<name>.cmd`` exists
      (common for npm/bun-installed CLIs). Routes through a shell so
      batch variables (``%dp0%``, ``%~dp0``) are expanded natively.
      Shell choice controlled by ``MODEX_EXTERNAL_SHELL``:

      - ``"cmd"`` (default) → ``cmd.exe /c <name>``
      - ``"powershell"`` → ``powershell.exe -NoProfile -Command <name>``
      - ``"none"`` → return ``name`` verbatim (let ``CreateProcess``
        try directly; useful when the .cmd is just a wrapper that
        CreateProcess happens to handle)

    4. **POSIX** — identity: ``name`` returned untouched.

    5. **Fallback** (Windows, neither .exe nor .cmd found) — return
      ``name`` verbatim; spawn will likely fail with a clear error.
    """
    env_override = os.environ.get(f"MODEX_{name.upper()}_EXECUTABLE")
    if env_override:
        return ResolvedExecutable(argv0=env_override)

    if _IS_POSIX:
        return ResolvedExecutable(argv0=name)

    if _find_on_path(name + ".exe") is not None:
        return ResolvedExecutable(argv0=name)

    if _find_on_path(name + ".cmd") is not None:
        real_exe = _resolve_cmd_shim(name)
        if real_exe is not None:
            if logger is not None:
                logger.info(
                    "Resolved %s.cmd shim to native exe: %s",
                    name,
                    real_exe,
                )
            return ResolvedExecutable(argv0=real_exe)

        shell = os.environ.get("MODEX_EXTERNAL_SHELL", "cmd").lower()
        if logger is not None:
            logger.info(
                "No native %s.exe on PATH; routing through %s.",
                name,
                shell,
            )
        if shell == "powershell":
            return ResolvedExecutable(
                argv0="powershell.exe",
                extra_args=("-NoProfile", "-Command", name),
            )
        if shell == "none":
            return ResolvedExecutable(argv0=name)
        return ResolvedExecutable(argv0="cmd.exe", extra_args=("/c", name))

    return ResolvedExecutable(argv0=name)


def _find_on_path(file_name: str) -> Path | None:
    path_env = os.environ.get("PATH", "")
    for entry in path_env.split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry) / file_name
        if candidate.is_file():
            return candidate
    return None


def _resolve_cmd_shim(name: str) -> str | None:
    shim = _find_on_path(name + ".cmd")
    if shim is None:
        return None
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    shim_dir = shim.parent.resolve()
    for line in text.splitlines():
        m = re.search(r'"([^"]+\.exe)"', line)
        if m:
            raw = m.group(1)
            raw = raw.replace("%dp0%", str(shim_dir)).replace("%~dp0%", str(shim_dir))
            exe_path = Path(raw)
            if not exe_path.is_absolute():
                exe_path = shim_dir / exe_path
            exe_path = exe_path.resolve()
            if exe_path.is_file():
                return str(exe_path)
    return None


# ---------------------------------------------------------------------------
# spawn_process_group
# ---------------------------------------------------------------------------


async def spawn_process_group(
    args: list[str],
    cwd: Path | None,
    env: dict[str, str],
    stdin: int | None,
    *,
    limit: int = 2**20,
) -> asyncio.subprocess.Process:
    """Spawn ``args`` as a new process-group leader.

    Args:
        args: argv vector (``argv0`` first).
        cwd: Working directory for the child. ``None`` inherits the
            parent process's cwd (use this when the child's routing is
            header-based, not cwd-based — e.g. the shared ``opencode
            serve`` process, which routes by ``x-opencode-directory``).
        env: Environment variables to pass to ``subprocess.Popen(env=...)``.
        stdin: ``asyncio.subprocess.PIPE`` (or any other int sentinel)
            to pipe stdin; ``None`` to inherit / close stdin. ``DEVNULL``
            and ``STDOUT`` are also valid.
        limit: Maximum line length for the stdout/stderr StreamReader.
            Default 1 MiB — the asyncio default of 64 KiB is too small
            for OpenCode JSONL lines carrying large tool outputs (file
            reads, search results).
    Returns:
        The running ``asyncio.subprocess.Process``. Its ``stdout`` and
        ``stderr`` are always piped (the provider parser needs the
        stdout side; stderr is captured so it can be folded into
        ``BackendResult.error`` on failure).
    """
    cwd_str = str(cwd) if cwd is not None else None
    if _IS_POSIX:
        return await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd_str,
            env=env,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=limit,
        )
    return await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd_str,
        env=env,
        stdin=stdin,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# terminate_process_group
# ---------------------------------------------------------------------------


async def terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    """Tear down ``proc`` and its entire process group.

    POSIX: graceful ``SIGTERM`` → ``asyncio.wait_for(proc.wait())``
    with a 1-second budget → hard ``SIGKILL``. Windows:
    ``taskkill /T /PID <pid>`` (``/T`` walks the tree). Both branches
    handle already-dead processes silently (``ProcessLookupError`` on
    POSIX; non-zero / not-found ``taskkill`` exit on Windows).
    """
    if proc.returncode is not None:
        return

    if _IS_POSIX:
        await _terminate_posix(proc)
        return
    if _IS_WINDOWS:
        await _terminate_windows(proc)
        return
    try:
        proc.terminate()
        await proc.wait()
    except (ProcessLookupError, OSError):
        pass


async def _terminate_posix(proc: asyncio.subprocess.Process) -> None:
    """POSIX branch of :func:`terminate_process_group`."""
    try:
        pgid = os.getpgid(proc.pid)  # type: ignore[attr-defined]
    except (ProcessLookupError, OSError):
        return

    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(pgid, signal.SIGTERM)  # type: ignore[attr-defined]

    try:
        await asyncio.wait_for(proc.wait(), timeout=1.0)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(pgid, signal.SIGKILL)  # type: ignore[attr-defined]
        with contextlib.suppress(ProcessLookupError, OSError, asyncio.CancelledError):
            await proc.wait()


async def _terminate_windows(proc: asyncio.subprocess.Process) -> None:
    """Windows branch of :func:`terminate_process_group`."""
    with contextlib.suppress(OSError):
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            check=False,
            capture_output=True,
        )
    with contextlib.suppress(
        ProcessLookupError,
        OSError,
        asyncio.CancelledError,
        TimeoutError,
    ):
        await asyncio.wait_for(proc.wait(), timeout=2.0)


# ---------------------------------------------------------------------------
# _sync_kill_proc — sync kill for finalize/atexit/signal context
# ---------------------------------------------------------------------------


def _sync_kill_proc(pid: int) -> None:
    """Synchronously kill a process by PID.

    Safe to call from ``__del__``, ``weakref.finalize``, ``atexit``,
    and signal handlers — no awaits, no logging, no propagated exceptions.

    POSIX: ``os.kill(pid, SIGKILL)``. Windows: ``taskkill /F /T /PID``.
    ``ProcessLookupError`` and ``OSError`` (process already dead, or
    ``taskkill`` not found) are suppressed.
    """
    try:
        if _IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        else:
            os.kill(pid, signal.SIGKILL)  # type: ignore[attr-defined]
    except (ProcessLookupError, OSError):
        pass


# ---------------------------------------------------------------------------
# register_signal_handlers — cooperative SIGTERM/SIGINT cleanup
# ---------------------------------------------------------------------------

_signal_handlers_registered: bool = False


def register_signal_handlers() -> None:
    """Register SIGTERM/SIGINT/SIGBREAK handlers that run atexit cleanup.

    Idempotent — safe to call multiple times. Cooperative — chains to
    the previous handler if non-default (checked via ``signal.getsignal``
    before registering). Called once at bot startup (T8's responsibility).

    The handler calls ``atexit._run_exitfuncs()`` (which runs all
    registered atexit hooks, including ``_atexit_cleanup`` from
    :mod:`opencode_server_backend`) then ``sys.exit(0)``.

    On Windows, ``SIGBREAK`` is registered in addition to ``SIGINT``
    because ``taskkill`` (without ``/f``) sends ``CTRL_BREAK_EVENT``,
    which Python maps to ``SIGBREAK`` — not ``SIGINT``. ``SIGTERM`` is
    registered too but is only meaningful on POSIX (Windows
    ``TerminateProcess`` bypasses all signal handlers).
    """
    global _signal_handlers_registered
    if _signal_handlers_registered:
        return
    _signal_handlers_registered = True

    def _signal_cleanup(signum: int, frame: object) -> None:
        atexit._run_exitfuncs()
        sys.exit(0)

    def _make_chained(prev: object) -> Callable[[int, object], None]:
        def _chained(signum: int, frame: object) -> None:
            if callable(prev):
                prev(signum, frame)
            _signal_cleanup(signum, frame)

        return _chained

    _sigs = [signal.SIGTERM, signal.SIGINT]
    if _IS_WINDOWS:
        _sigs.append(signal.SIGBREAK)
    for sig in _sigs:
        prev = signal.getsignal(sig)
        if prev == signal.SIG_DFL or prev is None:
            signal.signal(sig, _signal_cleanup)
        else:
            signal.signal(sig, _make_chained(prev))


__all__ = [
    "ResolvedExecutable",
    "resolve_executable",
    "spawn_process_group",
    "terminate_process_group",
    "register_signal_handlers",
]
