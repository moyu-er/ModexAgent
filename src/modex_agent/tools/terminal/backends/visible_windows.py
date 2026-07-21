"""Visible Windows PTY backend — subprocess with CREATE_NEW_CONSOLE.

Launches a helper process (visible_windows_host.py) that owns a visible
console window.  The helper creates a winpty.PtyProcess and forwards I/O
via a local TCP socket so the parent process and the visible window share
the exact same data stream.

ADR-0032 D2 (native-async escape hatch): both sides of the parent↔host IPC
bridge are rewritten from raw ``socket.socket`` + ``settimeout`` +
``sendall``/``recv`` to ``asyncio.start_server`` / ``asyncio.open_connection``
+ ``StreamReader`` / ``StreamWriter``. This structurally eliminates both
root causes that produced the "tab stuck" and "command typed but not
submitted" symptoms on the visible Windows path:

- No ``settimeout`` leak — asyncio streams have no per-call socket-timeout
  state to mutate. Read timeouts are expressed as
  ``asyncio.wait_for(reader.read(n), timeout=…)``.
- No partial ``sendall`` — ``writer.write()`` buffers the full payload in
  memory; ``await writer.drain()`` flushes to the socket. If the connection
  breaks, ``drain()`` raises ``ConnectionResetError`` — no partial bytes
  sent.

Per ADR-0032 D1 point 2, this backend overrides ``write`` and
``read_pending`` directly (native async) and does NOT implement the
``_write_blocking`` / ``_read_blocking`` hooks — ``StreamWriter`` /
``StreamReader`` is already an async transport, so wrapping it in the hook
template would double-wrap and lose the structural guarantee against
``settimeout``-style leaks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from modex_agent.tools.terminal.pty_keys import CTRL_C
from modex_agent.tools.terminal.results import SlidingOutputBuffer, TerminalRead
from modex_agent.tools.terminal.types import (
    Platform,
    ShellFamily,
    TerminalVisibility,
    _family_from_path,
)

from .winpty_transport import WinptyBackend

logger = logging.getLogger(__name__)

# Seconds before the parent gives up waiting for the host process to connect
# back to the ``asyncio.start_server`` listener.
_HOST_CONNECT_TIMEOUT = 10.0

# Capped wait for ``Server.wait_closed()`` after ``close()``. Per the asyncio
# contract, ``wait_closed()`` also waits for active connections to drop, but
# the accepted host connection is held by ``self._writer`` for the lifetime
# of the session — without this cap, ``start()`` hangs forever on Python 3.12+
# (which enforces the "wait for active connections" clause that 3.11 ignored).
_SERVER_CLOSE_TIMEOUT = 2.0


class WinptyConsoleWindowBackend(WinptyBackend):
    """WinptyConsoleWindowBackend — Windows visible console window backend.

    Renamed per ADR-0010 Decision 3 (transport-named subclasses). The legacy
    name ``VisibleWindowsPtyBackend`` is re-exported as a deprecated alias
    in ``backends/__init__.py`` for the migration window.
    """

    platform = Platform.WINDOWS
    visibility = TerminalVisibility.VISIBLE

    def __init__(self) -> None:
        super().__init__()
        self._proc: subprocess.Popen | None = None
        # asyncio streams — set by ``_on_client_connected`` when the host
        # process connects back to the ``asyncio.start_server`` listener.
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        # Serialises reads against the StreamReader. ``asyncio.wait_for``
        # cancelling ``reader.read()`` on timeout leaves the stream's
        # internal ``_wait_for_data`` future in a partially-waiting state,
        # so a subsequent concurrent read raises
        # ``RuntimeError: read() called while another coroutine is already
        # waiting for incoming data``. The lock is the standard workaround.
        self._read_lock: asyncio.Lock = asyncio.Lock()
        # Event set by ``_on_client_connected`` so ``start()`` can await
        # the host's connection without blocking the event loop.
        self._client_connected: asyncio.Event = asyncio.Event()
        self._shell: str | None = None
        self._title: str = "agent-terminal"
        self._output_buffer = SlidingOutputBuffer()

    @property
    def window_title(self) -> str:
        return self._title

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """Start the host process and accept its TCP connection back to us.

        Uses ``asyncio.start_server`` (ADR-0032 D2) so the accept is a
        genuinely non-blocking ``await``. The host process (launched with
        ``CREATE_NEW_CONSOLE``) connects back via ``asyncio.open_connection``
        in ``visible_windows_host.py``; when the connection lands, the
        ``_on_client_connected`` callback stores the streams and sets
        ``_client_connected``.
        """
        if shell is None:
            bash = shutil.which("bash")
            shell = bash if bash else "cmd.exe"
        self._shell = shell
        self._title = f"Agent: {self._shell}"

        # Reset the per-start connection event so a re-start works.
        self._client_connected = asyncio.Event()
        self._reader = None
        self._writer = None

        server = await asyncio.start_server(
            self._on_client_connected,
            host="127.0.0.1",
            port=0,
        )
        port = server.sockets[0].getsockname()[1]

        host_script = Path(__file__).parent / "visible_windows_host.py"
        cmd = [sys.executable, str(host_script), self._shell, str(port)]
        if cwd:
            cmd.append(cwd)
        self._proc = subprocess.Popen(
            cmd,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            cwd=cwd,
            env=env,
        )

        try:
            await asyncio.wait_for(
                self._client_connected.wait(),
                timeout=_HOST_CONNECT_TIMEOUT,
            )
        except TimeoutError:
            self._proc.kill()
            raise RuntimeError(
                f"Visible terminal host process did not connect within {_HOST_CONNECT_TIMEOUT}s"
            ) from None
        finally:
            server.close()
            # The accepted host connection is held by self._writer for the
            # session lifetime (it IS the IPC channel). Server.wait_closed()
            # also waits for active connections to drop, so it would hang
            # forever here. Cap the wait: server.close() already stops
            # accepting new connections, which is all we need.
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=_SERVER_CLOSE_TIMEOUT)
            except TimeoutError:
                pass

        logger.debug("Windows visible terminal started: %s", self._shell)

    def _on_client_connected(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """``asyncio.start_server`` callback — store streams and set TCP_NODELAY.

        ``TCP_NODELAY`` is set on the underlying socket so the command +
        ``"\\r"`` payload is delivered to the host as a single TCP segment
        (loopback, well under MSS) rather than being coalesced by Nagle's
        algorithm. This is part of the structural guarantee against the
        "command typed but not submitted" symptom (ADR-0032 D2).
        """
        self._reader = reader
        self._writer = writer
        sock: socket.socket | None = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._client_connected.set()

    # ------------------------------------------------------------------
    # Native async I/O (ADR-0032 D1 point 2 — escape hatch)
    # ------------------------------------------------------------------
    # ``write`` and ``read_pending`` are overridden directly with
    # ``StreamWriter`` / ``StreamReader``. The base-class ``_write_blocking``
    # / ``_read_blocking`` hooks are NOT implemented — this backend's
    # underlying I/O is already ``await``-shaped, so the hook template would
    # double-wrap and lose the structural guarantee against
    # ``settimeout``-style leaks.

    async def write(self, data: str) -> None:
        """Send input to the host via ``StreamWriter``.

        ``writer.write`` buffers the full payload in memory;
        ``await writer.drain()`` flushes to the socket. If the connection
        breaks, ``drain`` raises ``ConnectionResetError`` — no partial bytes
        are sent (ADR-0032 D2, structural elimination of root cause 2).
        """
        if self._writer is None:
            raise RuntimeError("PTY not started")
        self._writer.write(data.encode("utf-8"))
        await self._writer.drain()

    async def read_pending(
        self,
        timeout: float = 5.0,
        max_size: int = 65536,
    ) -> TerminalRead:
        """Read pending PTY output via ``StreamReader`` with a wait-for timeout.

        ``asyncio.wait_for`` wraps ``reader.read(n)`` in a ``Future``; the
        underlying transport's socket timeout is never mutated, so the
        ``settimeout`` leak (ADR-0032 root cause 2) cannot occur.

        Reads are serialised by ``self._read_lock`` to avoid the
        ``another coroutine is already waiting for incoming data`` race
        that occurs when a previous ``wait_for`` times out and leaves
        the reader's internal ``_wait_for_data`` future dangling.
        """
        if self._reader is None:
            return TerminalRead(stdout="", raw="")
        async with self._read_lock:
            try:
                raw: bytes = await asyncio.wait_for(
                    self._reader.read(max_size),
                    timeout=timeout,
                )
            except TimeoutError:
                return TerminalRead(stdout="", raw="")
        text = raw.decode("utf-8", errors="replace") if raw else ""
        if text:
            self._append_to_buffer(text)
        return TerminalRead(stdout=text, raw=text)

    def _shell_family(self) -> ShellFamily:
        """Return the shell family of the running shell (ADR-0032 D4.1)."""
        return _family_from_path(self._shell or "")

    # ------------------------------------------------------------------
    # Lifecycle continued
    # ------------------------------------------------------------------

    async def interrupt(self) -> None:
        await self.write(CTRL_C)

    def stdin_writable(self) -> bool:
        return self._writer is not None

    async def is_alive(self) -> bool:
        if self._proc is None:
            return False
        return self._proc.poll() is None

    async def terminate(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._writer = None
            self._reader = None
        if self._proc is not None:
            self._proc.terminate()

    async def kill(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._writer = None
            self._reader = None
        if self._proc is not None:
            self._proc.kill()
