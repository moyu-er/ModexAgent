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
|  CommandTool      | --> |  TerminalSession  | --> |  TerminalBackend  |
|  (agent-facing)   |     |  (session logic)  |     |  (OS process)     |
+-------------------+     +-------------------+     +-------------------+
         |                         |                         |
         |   execute("cmd")        |   write/read            |   PTY/tmux
         |                         |                         |
         v                         v                         v
   Returns output           Timeout/busy/ok           bash/cmd visible
```

### Layer 1: CommandTool (`modex_agent/tools/terminal/command_tool.py`)

The **agent-facing** shell execution tool for persistent terminal sessions.
It delegates to `TerminalSession` and keeps state across calls.

When terminal backends are unavailable, the application falls back to
`SubprocessTool` (`modex_agent/tools/terminal/subprocess_tool.py`), which runs
each command in a fresh process and does **not** preserve state.

Key features:
- Supports `^C` / `ctrl+c` / `\x03` as an interrupt command
- Timeout returns partial output + `<status>timeout</status>` XML
- Returns statuses: completed, executing, timed_out, paginated, waiting_input, stuck

### Layer 1b: SubprocessTool (fallback)

Registered when no terminal backend is available (e.g. subagents or when
`use_terminal=false`). Each invocation is stateless.

### Layer 2: TerminalSession (`modex_agent/tools/terminal/session.py`)

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

### Layer 3: BaseTerminalManager (`modex_agent/tools/terminal/managers.py`)

The **session registry** owning all named tabs (ADR-0010 two-axis: `shell_info`
× `visibility`). The legacy OS-named managers and the second `TerminalManager`
class have been folded inward into this single implementation; capability
behaviours (LRU, persistence, memory-pressure buffer clearing) are flag-guarded
private methods, default-off:

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

### Layer 4: TerminalTool (`modex_agent/tools/terminal/tool.py`)

The **agent-facing tab management tool**. Registered only when `BaseTerminalManager`
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

## How CommandTool and TerminalTool Work Together

### Normal Flow

```
Agent calls bash.execute("ls -la")
  -> CommandTool
    -> manager.get_default_session() [creates "default" if none]
    -> session.submit_command("ls -la")        # writes command + line ending
    -> poll_until_settled(session, ...)         # shared poll loop (poll_loop.py)
    -> session.apply_outcome(poll_result)       # writes busy/last_status state
  -> returns plain output (sanitized)
```

### Timeout Recovery Flow

```
Agent calls bash.execute("sleep 100")
  -> poll_until_settled hits timeout
  -> TIMED_OUT terminates the session (backend killed)
  -> apply_outcome(TIMED_OUT) sets _busy_after_timeout / last_status="timeout"

Agent calls bash.execute("echo next")
  -> CommandTool input guard rejects: session is busy (TIMED_OUT)
  -> returns <status>timeout</status> diagnostic suggesting ^C

Agent calls process.write("^C")      # ProcessTool — ^C goes through it
  -> session._busy_after_timeout cleared
  -> session is writable again

Agent calls bash.execute("echo done")
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

### Windows: WinptyConsoleWindowBackend

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
`create_pty_backend()` checks pexpect availability first; if unavailable, falls back to tmux. The
single `BaseTerminalManager` (two-axis `shell_info` × `visibility`) auto-detects available backends
through the factory.

### Windows: WinptyConsoleWindowBackend + WinptyHiddenBackend

Two Windows backends: visible (OS console window, human can observe/intervene) and hidden (headless, no visible window).
`create_pty_backend()` on Windows uses `WinptyHiddenBackend` by default, selecting the transport from
the platform + `visibility` axis (legacy aliases `VisibleWindowsPtyBackend` / `WindowsHiddenPtyBackend`
are re-exported in `backends/__init__.py` for the migration window).

### Fallback: SubprocessExecutor

When bash is unavailable or `use_terminal=false`, the bot falls back to
`SubprocessTool` — each command runs in a fresh process, no state persists.

---

## Bot Project Integration

`examples/bot_project/config/bot_config.yml`:

```yaml
main:
  use_terminal: true   # Enable CommandTool/ProcessTool/TerminalTool

terminal:
  close_on_exit: false  # Keep tabs open after bot shutdown
```

`examples/bot_project/bot/service/core.py`:
- Detects bash via `detect_platform_shell()` — uses WSL bash on Windows,
  falls back to `SubprocessTool` if bash unavailable
- Creates a terminal manager with shell detection result
- Registers `CommandTool` for persistent command execution
- Registers `ProcessTool` for interacting with running commands
- Registers `TerminalTool` when the terminal manager exists

`examples/bot_project/bot/service/builders.py`:
- `_make_shell_tool()` returns `SubprocessTool` for agents without terminal support

---

## Key Behaviors and Constraints

1. **Bash-only on Windows**: Bot project enforces WSL bash or Git Bash via `detect_platform_shell()`. CMD and PowerShell are not supported as terminal shells; if no bash is available the pool falls back to `SubprocessTool`.
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
| `manager.py` | Deprecated alias — `TerminalManager = BaseTerminalManager` (re-exported for the migration window) |
| `managers.py` | `BaseTerminalManager` (single two-axis impl: shell_info × visibility, with folded flag-guarded LRU / persistence / memory-pressure) + `create_terminal_manager` factory |
| `session.py` | Per-tab execution, timeout XML, cleanup, history |
| `tool.py` | LLM terminal management tool (open/close/list/select/interrupt) |
| `process_tool.py` | Process management tool — write/submit/send_keys/kill running commands |
| `process_registry.py` | Process tracking and registry |
| `command_tool.py` | Command execution tool — submits commands with input guard |
| `subprocess_tool.py` | Stateless `bash` fallback — fresh process per call |
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
| `backends/windows_hidden.py` | `WinptyHiddenBackend` (legacy alias `WindowsHiddenPtyBackend`) — hidden Windows PTY backend |
| `backends/pexpect_pty.py` | `PexpectPtyBackend` — Linux native PTY via pexpect |
| `backends/tmux_pty.py` | `TmuxPtyBackend` — Unix tmux backend |
