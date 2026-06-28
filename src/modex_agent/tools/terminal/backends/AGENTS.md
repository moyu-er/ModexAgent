<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# backends

## Purpose
Platform-specific terminal backend implementations that provide the OS-level PTY (pseudo-terminal) process management. Supports Windows (visible console window and hidden in-process) and Unix (pexpect hidden, tmux visible/hidden). Each backend implements the `TerminalBackend` ABC and is selected by `create_pty_backend()` based on the current platform.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `base.py` | `TerminalBackend` ABC — defines the interface for all PTY backends: `start()`, `write()`, `read()`, `resize()`, `close()`, plus `extract_current_segment_from_buffer()` utility for extracting terminal output segments |
| `factory.py` | `create_pty_backend()` — platform-aware factory. Windows: `WinptyConsoleWindowBackend` (legacy alias `VisibleWindowsPtyBackend`). Linux/macOS: `PexpectPtyBackend` (preferred), `TmuxPtyBackend` (fallback) |
| `pexpect_pty.py` | `PexpectPtyBackend` — Linux/macOS hidden PTY using pexpect in-process. No visible window. Modeled on `WinptyHiddenBackend` (legacy alias `WindowsHiddenPtyBackend`) for behavioral consistency |
| `tmux_pty.py` | `TmuxPtyBackend` — unified Unix backend using tmux + libtmux. Supports both headless and visible modes (users attach via `tmux attach -t <session>`) |
| `visible_windows.py` | `WinptyConsoleWindowBackend` (legacy alias `VisibleWindowsPtyBackend`) — Windows backend with visible console window. Launches a helper process (`visible_windows_host.py`) that owns a winpty and forwards I/O via TCP socket |
| `visible_windows_host.py` | `WinptyConsoleWindowBackend` host process — runs in a visible console window, creates a winpty `PtyProcess`, and bridges I/O with the parent process over a local TCP socket |
| `windows_hidden.py` | `WinptyHiddenBackend` (legacy alias `WindowsHiddenPtyBackend`) — Windows hidden terminal using pywinpty in-process. No visible console window. No helper subprocess or TCP bridge. Simpler than the visible backend |

## For AI Agents

### Working In This Directory
- Backends are **not** called directly by agents — they are used through `TerminalSession` (in `modex_agent/tools/terminal/`)
- `create_pty_backend()` selects the appropriate backend for the current platform automatically
- Platform detection: `Platform.WINDOWS` vs `Platform.LINUX`/`Platform.MACOS` (enum in `modex_agent/tools/terminal/types.py`)
- Visibility: `TerminalVisibility.HIDDEN` (pexpect, windows_hidden), `TerminalVisibility.VISIBLE` (visible_windows, tmux with visible pane)
- All backends implement: `start()`, `write(data)`, `read(timeout)` → `TerminalRead`, `close()`, `resize(rows, cols)`

### Common Patterns
- Backend lifecycle: `start()` → `write()`/`read()` loop → `close()`
- Each backend manages its own `SlidingOutputBuffer` for accumulating output between reads
- ANSI/CSI control sequences are stripped before returning output text
- Backend health is checked lazily (on use, not on idle)
- Windows visible backend uses TCP socket communication with a helper process — the host process writes data to the socket, the parent reads from it

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
