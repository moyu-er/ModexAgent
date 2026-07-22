<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# backends

## Purpose
Platform-specific terminal backend implementations that provide the OS-level PTY (pseudo-terminal) process management. Supports Windows (visible console window and hidden in-process) and Unix (pexpect hidden, tmux visible/hidden). Each backend implements the `TerminalBackend` ABC and is selected by `create_pty_backend()` based on the current platform.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init; re-exports deprecated aliases `VisibleWindowsPtyBackend` / `WindowsHiddenPtyBackend` |
| `base.py` | `TerminalBackend` ABC — defines the interface for all PTY backends. Per ADR-0032: concrete `write`/`read_pending` template methods wrap opt-in `_write_blocking`/`_read_blocking` hooks in `run_in_executor`; concrete `current_segment`/`clear_input_line`/`drain_startup` byte-stream defaults; `@abstractmethod _shell_family() -> ShellFamily` gates readline-dependent behaviors |
| `factory.py` | `create_pty_backend()` — platform-aware factory. Windows: `WinptyConsoleWindowBackend` (visible) / `WinptyHiddenBackend` (hidden). Linux/macOS: `PexpectPtyBackend` (hidden, preferred), `TmuxPtyBackend` (fallback, both visibilities). Raises `UnsupportedVisibilityForTransport` for invalid (transport, visibility) combos |
| `pexpect_pty.py` | `PexpectPtyBackend` — Linux/macOS hidden PTY using pexpect. Implements `_write_blocking`/`_read_blocking`/`_shell_family` hooks; inherits `write`/`read_pending`/`current_segment`/`clear_input_line`/`drain_startup` from base (ADR-0032 D3) |
| `tmux_pty.py` | `TmuxPtyBackend` — Unix tmux backend (snapshot backend, ADR-0032 D5). Overrides `write`/`read_pending`/`current_segment`/`drain_startup` directly (no hooks). `capture-pane -p -S -` + prefix-match diff (D5 Fix 1); `is_alive` 1s TTL cache (D5 Fix 2). Implements `_shell_family` |
| `visible_windows.py` | `WinptyConsoleWindowBackend` (visible) — parent side. ADR-0032 D2: `asyncio.start_server` + `StreamReader`/`StreamWriter` (was: raw `socket.socket` + `settimeout` + `sendall`/`recv`). Overrides `write`/`read_pending` directly (native async, no hooks). `TCP_NODELAY` set. Implements `_shell_family` |
| `visible_windows_host.py` | Host process for `WinptyConsoleWindowBackend` — runs in visible console window with `CREATE_NEW_CONSOLE`. ADR-0032 D2: `asyncio.open_connection`; `pty_to_socket`/`socket_to_pty` are asyncio tasks (blocking pywinpty calls wrapped in `run_in_executor`); `_stdin_to_pty`/`_resize_monitor` remain threads |
| `windows_hidden.py` | `WinptyHiddenBackend` (hidden) — in-process pywinpty. Implements `_write_blocking`/`_read_blocking`/`_shell_family` hooks; inherits 5 behaviors from base (ADR-0032 D3) |
| `winpty_transport.py` | `WinptyBackend` umbrella ABC for the Windows winpty transport (no I/O logic; exists for factory capability-table naming) |

## For AI Agents

### Working In This Directory
- Backends are **not** called directly by agents — they are used through `TerminalSession` (in `modex_agent/tools/terminal/`)
- `create_pty_backend()` selects the appropriate backend for the current platform automatically
- Platform detection: `Platform.WINDOWS` vs `Platform.LINUX`/`Platform.MACOS` (enum in `modex_agent/tools/terminal/types.py`)
- Visibility: `TerminalVisibility.HIDDEN` (pexpect, windows_hidden), `TerminalVisibility.VISIBLE` (visible_windows, tmux with visible pane)
- All backends implement: `start()`, `write(data)`, `read(timeout)` → `TerminalRead`, `close()`, `resize(rows, cols)`

### Common Patterns
- Backend lifecycle: `start()` → `write()`/`read()` loop → `close()`
- **Async-safety contract (ADR-0032)**: every backend's `write`/`read_pending` is genuinely non-blocking. Three I/O shapes: blocking-IO hooks (hidden-windows, pexpect), native async (visible-windows), snapshot (tmux). See `base.py` docstrings.
- Each backend manages its own `SlidingOutputBuffer` for accumulating output between reads (byte-stream backends only; tmux uses `_last_capture` diff)
- ANSI/CSI control sequences are stripped before returning output text
- Backend health is checked lazily (on use, not on idle)
- **Visible-windows IPC (ADR-0032 D2)**: asyncio streams (`asyncio.start_server`/`open_connection` + `StreamReader`/`StreamWriter`) with `TCP_NODELAY` — no raw socket, no `settimeout` leak, no partial `sendall`
- **Tmux snapshot backend (ADR-0032 D5)**: `capture-pane -p -S -` (full scrollback) + prefix-match diff; `is_alive` 1s TTL cache
- Shell detection via `_shell_family()` abstract hook (replaces 3 divergent pre-ADR-0032 heuristics)

## Dependencies

### Internal
- `modex_agent.tools.terminal.base` — `TerminalBackend` ABC
- `modex_agent.tools.terminal.results` — `SlidingOutputBuffer`, `TerminalRead`, `TerminalSegment`
- `modex_agent.tools.terminal.types` — `Platform`, `TerminalVisibility`
- `modex_agent.tools.terminal.prompt` — `drain_windows_startup`, `is_prompt_ready`
- `modex_agent.tools.terminal.pty_keys` — `CTRL_C`

### External
- `pexpect` (Unix, optional) — pexpect-based PTY
- `libtmux` (Unix, optional) — tmux control
- `pywinpty` (Windows, optional) — winpty-based PTY (hidden)
- `winpty` (Windows, optional) — winpty-based PTY (visible, via host process)

<!-- MANUAL -->
