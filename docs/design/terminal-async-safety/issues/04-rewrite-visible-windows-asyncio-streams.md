# 04 — Rewrite `WinptyConsoleWindowBackend` with asyncio native streams

**What to build:** The highest-value ticket. Rewrite both sides of the visible-windows IPC bridge — parent process (`visible_windows.py`) and host process (`visible_windows_host.py`) — from raw `socket.socket` + `socket.settimeout` + `socket.sendall`/`recv` to `asyncio.start_server` / `asyncio.open_connection` + `StreamReader` / `StreamWriter`. This structurally eliminates both root causes that produced the "页签卡住" and "指令输入到一半卡住需要手动 Enter" symptoms on the visible Windows path:

- **No `settimeout` leak** — asyncio streams have no per-call socket-timeout state to mutate. Read timeouts are expressed as `asyncio.wait_for(reader.read(n), timeout=…)`, which wraps in a `Future` without touching the transport.
- **No partial `sendall`** — `writer.write()` buffers the full payload in memory; `await writer.drain()` flushes to the socket. If the connection breaks, `drain()` raises `ConnectionResetError` — no partial bytes sent. The OS send buffer receives the full `command + "\r"` as one TCP segment (loopback, well under MSS).

Concretely:

### Parent side (`backends/visible_windows.py`)

- Replace `socket.socket` server creation + `server.accept()` with `asyncio.start_server`.
- Replace `self._sock.sendall(data.encode())` in `write()` with `self._writer.write(data.encode()); await self._writer.drain()`. Override `write` directly (no `_write_blocking` — this is the D1 point 2 native-async escape hatch).
- Replace `self._sock.recv` / `settimeout` in `read()` / `read_pending()` with `await asyncio.wait_for(self._reader.read(n), timeout=…)`. Override `read_pending` directly.
- Set `TCP_NODELAY` on the underlying transport (via `transport.set_tcp_nodelay(True)` or `sock.setsockopt(IPPROTO_TCP, TCP_NODELAY, 1)` after connection).
- Delete `socket.settimeout` calls entirely — no leak surface remains.
- Delete the `async def current_segment`, `async def clear_input_line`, `async def drain_startup` overrides — inherited from base class.
- Delete `_uses_readline` private helper (if present) — `_shell_family` replaces it.
- Implement `_shell_family` — `return _family_from_path(self._shell or "")`.
- Keeps `start`, `interrupt`, `is_alive`, `terminate`, `kill`, `window_title` (signatures unchanged; `start` switches to `asyncio.start_server` internally).

### Host side (`backends/visible_windows_host.py`)

- Replace `socket.connect((host, port))` with `asyncio.open_connection(host, port)` returning `(reader, writer)`.
- Convert the two forwarding threads (`pty_to_socket`, `socket_to_pty`) to asyncio tasks running in a single event loop in the host process. `pty_to_socket` becomes `async def pty_to_socket()` that awaits PTY reads (still via `proc.fileobj.recv` wrapped in `run_in_executor` — pywinpty is not asyncio-native) and writes to `writer`. `socket_to_pty` becomes `async def socket_to_pty()` that awaits `reader.read(n)` and writes to the PTY (via `run_in_executor`).
- Keep `_stdin_to_pty` (the human-keyboard input forwarder) and `_resize_monitor` as threads — they don't touch the socket and their threading is correct.
- Set `TCP_NODELAY` on the host-side socket as well.
- CRLF→LF normalization (`text.replace("\r\n", "\n")`) before writing to stdout is preserved.
- The `isalive()` check that terminates `pty_to_socket` when the shell dies is preserved.

Add `tests/unit/tools/terminal/backends/test_visible_windows.py`:

- Test the parent-side `write` calls `writer.write` + `drain` (mock the `StreamWriter`).
- Test the parent-side `read_pending` honors timeout via `asyncio.wait_for` (no `settimeout` mutation).
- Test `_shell_family` returns correct `ShellFamily` for bash / cmd / unknown paths.
- Test that `start` uses `asyncio.start_server` (structural assertion).
- Test connection-reset handling on both sides: parent side raises `ConnectionResetError` on `drain` (no partial write); host side task exits cleanly.
- Test `TCP_NODELAY` is set on the parent-side transport (via mock or assertion on `transport.set_tcp_nodelay` call).

Update the integration test `tests/framework/tools/terminal/test_windows_terminal_command_process_workflow.py`:

- Un-skip the `TerminalVisibility.VISIBLE` parametrization that was previously marked "flaky" (the comment says "PTY output capture timing is inherently flaky across shells"). After this ticket the asyncio-streams path should make VISIBLE reliable. If it remains flaky for unrelated reasons, document the specific remaining flakiness in a comment — do NOT re-skip without justification.

This ticket does NOT touch the hidden-windows, pexpect, or tmux backends.

**Blocked by:** 01 — Prefactor: expand `TerminalBackend` with async-safety scaffolding.

**Status:** ready-for-agent

- [ ] Parent side (`visible_windows.py`) uses `asyncio.start_server` + `StreamReader`/`StreamWriter`; no raw `socket.socket`, no `settimeout`, no `sendall`/`recv`
- [ ] Host side (`visible_windows_host.py`) uses `asyncio.open_connection`; the two forwarding threads become asyncio tasks in a single event loop; `_stdin_to_pty` and `_resize_monitor` remain threads
- [ ] `TCP_NODELAY` set on both parent and host transports
- [ ] `WinptyConsoleWindowBackend` overrides `write`/`read_pending` directly with native async (no `_write_blocking`/`_read_blocking` — D1 point 2 escape hatch)
- [ ] `WinptyConsoleWindowBackend` deletes `current_segment`/`clear_input_line`/`drain_startup` overrides (inherited from base); deletes `_uses_readline` (replaced by `_shell_family`)
- [ ] `WinptyConsoleWindowBackend` implements `_shell_family`
- [ ] `tests/unit/tools/terminal/backends/test_visible_windows.py` covers write/drain, read-with-timeout, shell-family, connection-reset, TCP_NODELAY
- [ ] `tests/framework/tools/terminal/test_windows_terminal_command_process_workflow.py` VISIBLE parametrization un-skipped and passing (or remaining flakiness documented with specific cause)
- [ ] `ruff check src/modex_agent/tools/terminal/backends/visible_windows.py src/modex_agent/tools/terminal/backends/visible_windows_host.py` clean
- [ ] `mypy src/modex_agent/tools/terminal/backends/visible_windows.py src/modex_agent/tools/terminal/backends/visible_windows_host.py` clean

## Comments

- This is the largest ticket in the sequence — two-file IPC rewrite, ~650 lines touched. If the parent/host split felt natural to break into two tickets it would, but the IPC protocol changes on both sides must land together (a half-migrated bridge cannot pass bytes), so it stays as one.
- ADR-0032 D2 mandates this rewrite; the alternative "minimal patch" (wrap `sendall`/`recv` in `run_in_executor` + reset `settimeout(None)` before each `sendall`) was rejected because it leaves the `run_in_executor` thread-pool pattern and raw-socket fragility in place.
- The visible-windows backend keeps overriding `write`/`read_pending` directly (no blocking-IO hooks) because asyncio `StreamWriter`/`StreamReader` is already a native async transport — wrapping it in `_write_blocking` + `run_in_executor` would double-wrap and lose the structural guarantee against `settimeout`-style leaks.
- Manual verification (beyond automated tests): run a real `bash.execute("ls")` against the visible Windows backend and confirm (a) the command text + `\r` arrives atomically in the host (visible in the console window), (b) no manual Enter is required, (c) the WebUI does not freeze during a long-running command.
