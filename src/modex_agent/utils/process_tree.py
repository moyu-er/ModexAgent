"""Cross-platform subprocess-tree termination."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from typing import Final

_IS_WINDOWS: Final[bool] = sys.platform == "win32"
_IS_POSIX: Final[bool] = sys.platform != "win32"


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


__all__ = ["terminate_process_group"]
