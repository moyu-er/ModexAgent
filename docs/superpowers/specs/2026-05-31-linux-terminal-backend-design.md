# Linux Terminal Backend — PexpectPtyBackend + Unified Degradation Chain

**Date**: 2026-05-31
**Status**: draft
**Scope**: `framework/tools/terminal/` + `examples/bot_project/bot/service/`

## Problem

1. No native Linux PTY backend exists — `TmuxPtyBackend` is the only Unix option,
   requiring both `tmux` binary and `libtmux` Python package.  If either is
   missing the terminal system crashes with `ImportError` or `RuntimeError`;
   there is no graceful degradation to `SubprocessTool`.

2. `create_terminal_manager()` only accepts `"windows_hidden"` or
   `"windows_visible"` — there is no `LinuxTerminalManager` class.

3. `pool_builder._create_terminal_manager()` calls
   `create_terminal_manager(manager_kind="windows_visible")` unconditionally,
   which crashes on non-Windows platforms even when `tmux` is available.

4. `TerminalManager` (full version in `manager.py`) calls
   `create_pty_backend()` which creates `TmuxPtyBackend` with a hard
   `ImportError` at construction time — no fallback.

## Design

### Unified Degradation Chain (All Platforms)

```
Platform detect
  │
  ├─ Windows:
  │   1. VisibleWindowsPtyBackend  (visible)
  │   2. WindowsHiddenPtyBackend   (hidden, winpty)
  │   3. None → SubprocessTool     (no terminal tools registered)
  │
  └─ Linux / macOS:
      1. PexpectPtyBackend         (hidden, pexpect)          ← NEW
      2. TmuxPtyBackend            (hidden, tmux + libtmux)   ← existing
      3. None → SubprocessTool     (no terminal tools registered)
```

**Key invariant**: When `terminal_manager is None`, callers must NOT register
`CommandTool`, `ProcessTool`, or `TerminalTool`.  They must register only
`SubprocessTool`.  This invariant already holds in `pool_builder.py:311-324`
and `builders.py:180-192`; this work only fixes the Linux path so `None` is
actually reachable.

### Component Overview

```
┌────────────────────────────────────────────────────┐
│  BotService / PoolBuilder                          │
│  _create_terminal_manager() → auto-detect platform │
│  Returns TerminalManagerBase | None                │
├────────────────────────────────────────────────────┤
│  create_terminal_manager(kind, config)             │
│  Dispatches: "windows_visible" | "windows_hidden"  │
│              "linux"                                │
├────────────────────────────────────────────────────┤
│  LinuxTerminalManager(BaseTerminalManager)         │
│  Tries pexpect → tmux → returns None               │
├────────────────────────────────────────────────────┤
│  PexpectPtyBackend(TerminalBackend)     ← NEW      │
│  TmuxPtyBackend(TerminalBackend)       ← existing  │
└────────────────────────────────────────────────────┘
```

### New: PexpectPtyBackend

**File**: `framework/tools/terminal/backends/pexpect_pty.py`

Patterned on `WindowsHiddenPtyBackend` for behavioral consistency:

| Aspect | PexpectPtyBackend | WindowsHiddenPtyBackend |
|--------|-------------------|-------------------------|
| PTY library | `pexpect` | `winpty` |
| Process model | in-process | in-process |
| Visibility | HIDDEN | HIDDEN |
| `start()` | `pexpect.spawn(shell, dimensions=(30,120))` | `winpty.PtyProcess.spawn(shell, dimensions=(30,120))` |
| `write()` | `proc.send(data)` (no enter) | `proc.write(data)` |
| `read()` | `proc.read_nonblocking(size, timeout)` — **no buffer** | `fileobj.recv(max_size)` — buffer in read_pending |
| `read_pending()` | `read()` + `_append_to_buffer()` | `read()` + `_append_to_buffer()` |
| `current_segment()` | `extract_current_segment_from_buffer(buffer.text)` | same |
| `interrupt()` | `proc.sendintr()` | `proc.sendintr()` |
| `is_alive()` | `proc.isalive()` | `proc.isalive()` |
| `terminate()` | `proc.terminate(force=False)` | same |
| `kill()` | `proc.terminate(force=True)` | same |
| `drain_startup()` | poll `read()` until `is_prompt_ready()` (max 8s) | `drain_windows_startup()` |
| `clear_input_line()` | `\x01\x0b` for readline, no-op otherwise | same |

`drain_startup()` on Linux does NOT call `drain_windows_startup()` (which
references `winpty`-specific behavior).  Instead it implements the same
semantics: poll-read until a prompt is detected via `is_prompt_ready()`,
then drain remaining buffered output for readline shells.

### New: LinuxTerminalManager

**File**: `framework/tools/terminal/managers.py`

```python
class LinuxTerminalManager(BaseTerminalManager):
    """Terminal manager for Linux/macOS headless PTY sessions.

    Eagerly validates that at least one backend is available during __init__.
    If neither pexpect nor tmux+libtmux is importable, __init__ raises
    RuntimeError so the caller falls back to SubprocessTool.

    Degradation chain (enforced lazily per-session): pexpect → tmux.
    """

    def __init__(self, config: TerminalRuntimeConfig | None = None) -> None:
        shell_info = detect_platform_shell()  # may be None
        super().__init__(
            shell_info=shell_info or ShellInfo(
                family=ShellFamily.BASH, path="/bin/sh",
                platform=Platform.LINUX,
            ),
            visibility=TerminalVisibility.HIDDEN,
            backend_factory=_create_linux_backend,
            config=config,
        )
        # Eager validation: ensure at least one backend is available now.
        # If both fail, let RuntimeError propagate so caller returns None.
        _create_linux_backend()
```

`_create_linux_backend()` (module-level in `managers.py`) tries pexpect import
→ returns `PexpectPtyBackend()`, falls back to `TmuxPtyBackend()`, raises
`RuntimeError` if neither is available.  Called eagerly in `__init__` to detect
unavailability at pool startup rather than at first command; also used as the
lazy `backend_factory` for new sessions.

### Changes to create_terminal_manager()

Add `"linux"` kind:

```python
def create_terminal_manager(*, manager_kind, config=None):
    if manager_kind == "windows_hidden":
        return WindowsHiddenTerminalManager(config=config)
    if manager_kind == "windows_visible":
        return WindowsVisibleTerminalManager(config=config)
    if manager_kind == "linux":
        return LinuxTerminalManager(config=config)
    raise ValueError(f"Unsupported terminal manager kind: {manager_kind}")
```

### Changes to pool_builder._create_terminal_manager()

Replace the Windows-only logic with platform auto-detection:

```python
def _create_terminal_manager(pool_cfg, project_dir):
    use_terminal = any(getattr(a, "use_terminal", False) for a in pool_cfg.agents)
    if not use_terminal:
        return None

    import sys
    from framework.tools.terminal.managers import create_terminal_manager

    if sys.platform == "win32":
        kinds = ["windows_visible", "windows_hidden"]
    else:
        kinds = ["linux"]

    for kind in kinds:
        try:
            return create_terminal_manager(manager_kind=kind)
        except Exception:
            continue
    return None
```

### Changes to BotService core.py

The pipeline-mode path in `core.py:282-303` already has correct exception
handling around `TerminalManager()`.  But `TerminalManager` (the full version
in `manager.py`) calls `create_pty_backend()` which is platform-aware and will
pick `TmuxPtyBackend` on Linux.  On Linux, make it try pexpect first:

Update `create_pty_backend()` in `factory.py`:

```python
def create_pty_backend() -> TerminalBackend:
    if sys.platform == "win32":
        from .visible_windows import VisibleWindowsPtyBackend
        return VisibleWindowsPtyBackend()

    # Linux/macOS: pexpect preferred, tmux fallback
    try:
        from .pexpect_pty import PexpectPtyBackend
        return PexpectPtyBackend()
    except ImportError:
        pass
    from .tmux_pty import TmuxPtyBackend
    return TmuxPtyBackend()
```

The bot_project pipeline-mode path already wraps `TerminalManager()` in
try/except and sets `terminal_manager = None` on failure, so this is safe.

### Fix: WindowsHiddenPtyBackend.read()

`WindowsHiddenPtyBackend` does NOT override `read()`, so `base.read()` calls
`read_pending()` which buffers via `_append_to_buffer()`.  Meanwhile
`VisibleWindowsPtyBackend` overrides `read()` to read directly from the socket
without buffering.  This means `drain_startup()` buffers startup output for
hidden backends but not visible ones.

Fix: Override `read()` in `WindowsHiddenPtyBackend` to read directly from the
PTY fileobj without buffering, matching `VisibleWindowsPtyBackend`'s pattern:

```python
async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
    if self._proc is None:
        raise RuntimeError("PTY not started")
    loop = asyncio.get_running_loop()
    def _do_read() -> str:
        fobj = self._proc.fileobj
        fobj.settimeout(timeout)
        try:
            raw = fobj.recv(max_size)
            return raw.decode("utf-8", errors="replace")
        except (socket.timeout, TimeoutError, OSError):
            return ""
    try:
        return await loop.run_in_executor(None, _do_read)
    except Exception:
        return ""
```

And `read_pending()` calls `read()` then buffering, same as visible.

### Failure Modes

| Scenario | Behavior |
|----------|----------|
| `pexpect` installed, shell found | `PexpectPtyBackend` created, terminal tools registered |
| `pexpect` missing, `tmux` + `libtmux` installed | `TmuxPtyBackend` created, terminal tools registered |
| Neither `pexpect` nor `tmux` available | `terminal_manager = None`, only `SubprocessTool` registered |
| `use_terminal = false` in config | `terminal_manager = None`, only `SubprocessTool` |
| Backend dies mid-session | `TerminalSession` auto-restarts on next `execute()` |
| `pexpect.spawn()` fails at runtime | Propagate error → session restart → if persistent, agent sees error |

### Files Changed

| File | Change |
|------|--------|
| `framework/tools/terminal/backends/pexpect_pty.py` | **NEW** — PexpectPtyBackend |
| `framework/tools/terminal/backends/factory.py` | pexpect-first on Linux, tmux fallback |
| `framework/tools/terminal/backends/windows_hidden.py` | Override `read()` without buffering |
| `framework/tools/terminal/managers.py` | Add `LinuxTerminalManager`, `"linux"` kind |
| `examples/bot_project/bot/service/pool_builder.py` | Platform-auto-detection in `_create_terminal_manager()` |

### Not Changed

- `TerminalSession`, `CommandTool`, `ProcessTool`, `TerminalTool` — backend-agnostic
- `SubprocessTool`, `SubprocessExecutor` — existing fallback
- `pool_builder.py` tool registration condition (`if terminal_manager is not None`) — already correct
- `builders.py` `_register_tools()` — already correct
- `core.py` pipeline-mode terminal creation — already has try/except

### Tests

| File | What it covers |
|------|---------------|
| `tests/.../backends/test_pexpect_pty.py` | PexpectPtyBackend lifecycle: start/write/read/interrupt/is_alive/terminate/kill/drain_startup/clear_input_line. Mock `pexpect`. |
| `tests/.../test_linux_terminal_manager.py` | LinuxTerminalManager with mock backends, degradation when backends fail |
| `tests/.../backends/test_windows_hidden.py` | (existing) Update to verify `read()` does not buffer |
| `tests/.../test_terminal_degradation.py` | End-to-end: backend unavailable → None → SubprocessTool only |

### Acceptance Criteria

1. On Linux with `pexpect` installed: `PexpectPtyBackend` is used, terminal tools registered
2. On Linux without `pexpect` but with `tmux` + `libtmux`: `TmuxPtyBackend` is used
3. On Linux without either: `terminal_manager = None`, only `SubprocessTool` registered, no crash
4. On Windows: behavior unchanged (visible → hidden → None)
5. `use_terminal = false`: `terminal_manager = None` on all platforms
6. All existing tests continue to pass
7. New backend tests pass
