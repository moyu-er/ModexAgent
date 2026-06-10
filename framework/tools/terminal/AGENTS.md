<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-10 -->

# Terminal System — Agent Guide

## Overview

The terminal system provides **stateful, persistent shell sessions** for agents.
Unlike stateless subprocess execution (where each command runs in isolation),
this system keeps a real terminal process alive across tool calls, preserving:

- `cd` state and working directory
- Environment variables and aliases
- SSH connections and interactive sessions
- Command history and partial output on timeout

Each session is backed by an OS-visible terminal window when available,
allowing human users to observe or intervene in the same session.

---

## Architecture: Three Layers

```
+-------------------+     +-------------------+     +-------------------+
|  ShellTool        | --> |  TerminalSession  | --> |  TerminalBackend  |
|  (agent-facing)   |     |  (session logic)  |     |  (OS process)     |
+-------------------+     +-------------------+     +-------------------+
         |                         |                         |
         |   execute("cmd")        |   write/read            |   PTY/tmux
         |                         |                         |
         v                         v                         v
   Returns output           Timeout/busy/ok           bash/cmd visible
```

### Layer 1: ShellTool (`framework/tools/standard/shell_tool.py`)

The **agent-facing** shell execution tool. It decides whether to use:

- `TerminalSessionExecutor` — stateful, persistent sessions (preferred)
- `SubprocessExecutor` — stateless, fresh process per command (fallback)

The agent uses `shell` for normal command execution. It does **not** need to
open a terminal explicitly first — the first `shell.execute()` auto-creates a
default session.

Key features:
- Supports `^C` / `ctrl+c` / `\x03` as an interrupt command
- Dynamic description tells the model whether sessions are stateful or stateless
- Timeout returns partial output + `<status>timeout</status>` XML

### Layer 2: TerminalSession (`framework/tools/terminal/session.py`)

The **per-tab execution engine**. Each session wraps one backend and handles:

1. **Lazy startup** — backend is not started until first `execute()`
2. **Drain startup** — consume banner/prompt on newly started terminals
3. **Pre-execute cleanup** — clear readline input line (`Ctrl+A Ctrl+K`)
4. **Command submission** — write command with shell-appropriate line ending (`\n` for bash)
5. **Read loop** — accumulate output until prompt, waiting_input, death, or timeout
6. **State tracking**:
   - `_busy_after_timeout` — blocks new commands until interrupted
   - `_last_status` — `ok`, `timeout`, `waiting_input`, `ended`
7. **Output sanitization** — strip ANSI/CSI control sequences before returning to model
8. **History** — record truncated command + output for `TerminalTool.history`

Special states:

| Status | Meaning | Next action |
|--------|---------|-------------|
| `ok` | Command completed normally | Continue with next command |
| `timeout` | Command exceeded timeout, still running | Send `^C` to interrupt |
| `busy` | Previous command timed out, blocked | Send `^C` or `terminal.interrupt` |
| `waiting_input` | Command waiting for password/confirmation | Send input as next shell command |
| `ended` | Shell exited (`exit`/`logout`) | Session will auto-restart on next use |

### Layer 3: TerminalManager (`framework/tools/terminal/manager.py`)

The **session registry** owning all named tabs:

- **Named collection** — `name -> TerminalSession` map
- **Default terminal** — the tab `ShellTool` uses when no name is specified
- **LRU eviction** — closes oldest session when `max_terminals` exceeded
- **Lazy alive detection** — only checks backend health on use, not on idle
- **JSON persistence** — save/restore session metadata and history

Key methods:
- `get_or_create(name)` — get existing or create new, with LRU eviction
- `get_default_session()` — return the default tab (or None)
- `select_default(name)` — switch default tab
- `list_sessions()` — list all tabs with metadata; distinguishes between
  "unstarted" (backend never launched — kept alive) and "dead" (backend
  started then died — purged from registry)
- `close(name)` / `close_all()` — terminate sessions
- `save_state()` / `load_state()` — persist/restore

### Layer 4: TerminalTool (`framework/tools/terminal/tool.py`)

The **agent-facing tab management tool**. Registered only when `TerminalManager`
is available.

Actions:

| Action | Purpose | Required params |
|--------|---------|-----------------|
| `open` | Create a new named tab | `name` (optional, auto-generated) |
| `close` | Terminate a tab | `name` |
| `list` | Show all tabs with metadata | — |
| `select` | Switch default tab | `name` |
| `history` | Show recent output of a tab | `name` |
| `interrupt` | Send Ctrl+C to **default** tab | — |

**Important**: `terminal` does NOT execute commands — use `shell` for that.
The agent generally does NOT need to `open` a terminal before using `shell`.

---

## Input Guard (guard.py)

Before CommandTool or ProcessTool sends data to the terminal, a guard checks session readiness:

```
CommandTool.execute("ls")
  → check_command_writable(session)
    → session.command_status() → TerminalCommandStatus
    → allowed: IDLE, UNKNOWN, COMPLETED, TIMED_OUT
    → rejected: EXECUTING, LONG_RUNNING, STUCK, PAGINATED
  → None (ok) → proceed with command
  → TerminalGuardResult → return diagnostic to LLM with suggestion
```

```
ProcessTool._do_write(data)
  → check_process_writable(session)
    → allowed: IDLE, UNKNOWN, WAITING_INPUT, PAGINATED, COMPLETED, TIMED_OUT
    → rejected: EXECUTING, LONG_RUNNING, STUCK
  → None (ok) → write data
  → TerminalGuardResult → return diagnostic to LLM
```

Key difference: ProcessTool allows `WAITING_INPUT` (for typing passwords) and `PAGINATED` (for sending 'q'/Space), while CommandTool rejects both (a new command would corrupt the interaction).

---

## Poll Loop (poll_loop.py)

Both `CommandTool.execute()` and `ProcessTool._drain_terminal_after_action()` share a unified poll loop:

```
poll_until_settled(session, registry, proc_id, config, yield_ms=..., timeout_seconds=...)
  → poll-detect-yield cycle:
     1. Read available output
     2. Check for prompt detection (command finished)
     3. Check for input wait (password/confirmation)
     4. Check for pager entry (--help, less, etc.)
     5. Check for process exit
     6. Check stuck threshold (no output for configurable period)
     7. Check long-running threshold
     8. Check yield interval (return partial output for progress)
     9. Timeout expiry
```

`PollOutcome` captures the terminal state: PAGINATED is a new addition detecting pager programs (`less`, `more`, `--help` output) that require key input to dismiss.

---

## How ShellTool and TerminalTool Work Together

### Normal Flow

```
Agent calls shell.execute("ls -la")
  -> TerminalSessionExecutor
    -> TerminalManager.get_default_session() [creates "default" if none]
      -> TerminalSession.execute("ls -la")
        -> backend.write("ls -la\n")
        -> backend.read() until prompt detected
        -> return plain output
```

### Timeout Recovery Flow

```
Agent calls shell.execute("sleep 100")
  -> TerminalSession returns:
     <status>timeout</status>
     <message>Timed out after 60s...</message>

Agent calls shell.execute("echo next")
  -> TerminalSession returns:
     <status>busy</status>
     <message>Send ^C via shell tool or terminal.interrupt</message>

Agent calls shell.execute("^C")   # or terminal.interrupt
  -> session.send_interrupt() writes \x03
  -> busy state cleared

Agent calls shell.execute("echo done")
  -> Normal execution resumes
```

### SSH / Interactive Flow

```
Agent calls shell.execute("ssh root@host")
  -> TerminalSession returns:
     <status>waiting_input</status>
     <output>root@host's password:</output>

Agent calls shell.execute("mypassword")
  -> Input sent directly (no clear_input_line)
  -> Command continues
  -> Returns: "logged in\n$ "
```

### Multi-Tab Flow

```
Agent calls terminal.execute(action="open", name="remote")
  -> Creates "remote" tab

Agent calls terminal.execute(action="select", name="remote")
  -> Default tab switched to "remote"

Agent calls shell.execute("ssh user@server")
  -> Runs in "remote" tab

Agent calls terminal.execute(action="open", name="local")
Agent calls terminal.execute(action="select", name="local")
  -> Switch back to local work

Agent calls terminal.execute(action="interrupt")
  -> Sends Ctrl+C to current default ("local")
```

---

## Backends

### Windows: VisibleWindowsPtyBackend

Launches `visible_windows_host.py` in a new OS console window.

- Human can see and type in the visible window
- Agent commands also appear in the same window
- Parent process communicates via local TCP socket
- Uses `winpty.Backend.WinPTY` explicitly (avoids ConPTY DA1 pollution)
- Human Ctrl+C is handled by `SetConsoleCtrlHandler` (host survives)
- **Lifecycle fix**: `pty_to_socket()` treats empty `recv()` as timeout (continue polling) rather than breaking the thread, preventing premature I/O-forwarder death. `isalive()` check ensures host exits when shell dies.

Data flow:

```
agent parent -> socket -> visible host -> pywinpty -> bash
human keyboard -> visible host -> pywinpty -> bash
bash output -> pywinpty -> visible host -> socket -> agent
bash output -> pywinpty -> visible host -> stdout (visible window)
```

### Unix: PexpectPtyBackend (primary) + TmuxPtyBackend (fallback)

Linux uses a degradation chain: `PexpectPtyBackend` (native PTY via pexpect) → `TmuxPtyBackend` (tmux sessions).
`LinuxTerminalManager` auto-detects available backends and falls back gracefully.
`create_pty_backend()` checks pexpect availability first; if unavailable, falls back to tmux.

### Windows: VisibleWindowsPtyBackend + WindowsHiddenPtyBackend

Two Windows backends: visible (OS console window, human can observe/intervene) and hidden (headless, no visible window).
`WindowsTerminalManager` selects the appropriate backend.
`create_pty_backend()` on Windows uses `WindowsHiddenPtyBackend` by default.

### Fallback: SubprocessExecutor

When bash is unavailable or `use_terminal=false`, `ShellTool` falls back to
`SubprocessExecutor` — each command runs in a fresh process, no state persists.

---

## Bot Project Integration

`examples/bot_project/config/bot_config.yml`:

```yaml
main:
  use_terminal: true   # Enable TerminalSessionExecutor

terminal:
  close_on_exit: false  # Keep tabs open after bot shutdown
```

`examples/bot_project/bot/service/core.py`:
- Detects bash via `detect_platform_shell()` — uses WSL bash on Windows,
  falls back to `SubprocessExecutor` if bash unavailable
- Creates `TerminalManager` with shell detection result
- Registers `ShellTool` with `TerminalSessionExecutor`
- Registers `TerminalTool` when `TerminalManager` exists

`examples/bot_project/bot/service/builders.py`:
- `_make_shell_tool()` wires `TerminalSessionExecutor` or `SubprocessExecutor`

---

## Key Behaviors and Constraints

1. **Bash-only**: Bot project enforces WSL bash (detected via `detect_platform_shell()`). No CMD, no PowerShell, no Git Bash.
2. **Eager startup on open**: `TerminalTool.open` calls `ensure_started()` so visible windows appear immediately — no need to wait for first command.
3. **Unstarted sessions survive `list`**: A tab created by `open` but not yet
   used will show in `list` — it is not purged just because `is_alive()` is
   false before `start()`.
4. **CRLF normalization**: Windows console auto-expands `\n` to `\r\n`.
   PTY output with `\r\n` is normalized to `\n` before stdout write to prevent
   blank lines (`\r\r\n`).
5. **Output sanitization**: Model-facing output strips ANSI/CSI/DA1/OSC control
   sequences. Visible terminal output is NOT modified.
6. **Interrupt via both tools**: `shell.execute("^C")` and `terminal.interrupt`
   both work. `terminal.interrupt` targets the default tab without requiring a
   tab name.

---

## Files

| File | Purpose |
|------|---------|
| `manager.py` | Session registry, default terminal, LRU, persistence |
| `managers.py` | `WindowsTerminalManager`, `LinuxTerminalManager` — platform-specific manager variants with auto-detection |
| `session.py` | Per-tab execution, timeout XML, cleanup, history |
| `tool.py` | LLM terminal management tool (open/close/list/select/interrupt) |
| `process_tool.py` | Process management tool — list/kill processes |
| `process_registry.py` | Process tracking and registry |
| `command_tool.py` | Command execution tool — submits commands with input guard |
| `guard.py` | `TerminalGuard` — pre-flight input validation. `check_command_writable()` (CommandTool) and `check_process_writable()` (ProcessTool) enforce status-based allowlists; returns `TerminalGuardResult` with diagnostic `TerminalSnapshot` |
| `poll_loop.py` | Shared `poll_until_settled()` — reused by CommandTool and ProcessTool for post-write drain. `PollOutcome` enum (PROMPT_DETECTED / YIELDED / TIMED_OUT / INPUT_WAIT / STUCK / LONG_RUNNING / PROCESS_EXIT / PAGINATED) |
| `env.py` | `build_full_env()` — complete environment dict for child processes. On Windows, merges missing HKLM/HKCU PATH entries from registry |
| `prompt.py` | Prompt detection, ANSI/DA1 stripping, pager detection, startup drain |
| `types.py` | `ShellFamily`, `ShellInfo`, `Platform`, `detect_platform_shell` |
| `config.py` | Terminal configuration |
| `state_store.py` | Terminal state persistence |
| `results.py` | Result types |
| `pty_keys.py` | PTY key constants |
| `subprocess_tool.py` | Subprocess-based execution fallback |
| `backends/base.py` | `TerminalBackend` ABC |
| `backends/factory.py` | `create_pty_backend()` — platform-auto backend selection |
| `backends/visible_windows.py` | Visible Windows backend (winpty) — parent side |
| `backends/visible_windows_host.py` | Visible console helper process |
| `backends/windows_hidden.py` | `WindowsHiddenPtyBackend` — hidden Windows PTY backend |
| `backends/pexpect_pty.py` | `PexpectPtyBackend` — Linux native PTY via pexpect |
| `backends/tmux_pty.py` | `TmuxPtyBackend` — Unix tmux backend |
| `../standard/shell_tool.py` | LLM shell tool with executor selection |
