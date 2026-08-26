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
`PersistentBashTool` (`modex_agent/tools/terminal/persistent_bash.py`) —
one persistent interactive bash per conversation (stateful cwd/env,
routed by the caller's session_id), with `BashInputTool`
answering stdin-waiting commands. `SubprocessTool` (fresh process per
call) remains in the framework for direct callers/tests but is no longer
wired into any builder.

Key features:
- Supports `^C` / `ctrl+c` / `\x03` as an interrupt command
- Timeout returns partial output + `<status>timeout</status>` XML
- Returns statuses: completed, executing, timed_out, paginated, waiting_input, stuck

### Layer 1b: PersistentBashTool (fallback)

Registered when no terminal backend is available (e.g. subagents or when
`use_terminal=false`). One stateful bash per conversation (per-session_id
routing via `_current_session_id`; `__default__` shell without a routing
context); `bash_input` answers commands that block reading stdin.
POSIX-only (pexpect).

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
private methods. Memory-pressure defaults to on (global 1M cross-tab ceiling);
LRU and persistence remain opt-in:

- **Named collection** — `name -> TerminalSession` map
- **Default terminal** — the tab `ShellTool` uses when no name is specified
- **LRU eviction** — closes oldest session when `max_terminals` exceeded
- **Lazy alive detection** — only checks backend health on use, not on idle
- **JSON persistence** — save/restore session metadata and history

Key methods:
- `get_or_create(name)` — get existing or create new, with LRU eviction
- `get_default_session()` — return the default tab (or None)
- `select_default(name)` — switch default tab
- `list_sessions()` — list all tabs with metadata; dead tabs (backend started
  then died) are filtered out by `TerminalTool list` so the agent only sees
  active tabs
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
| `list` | Show all active tabs (dead tabs filtered out) | — |
| `select` | Switch default tab | `name` |
| `interrupt` | Send Ctrl+C to **default** tab | — |
| `current` | Show current default tab status and output | — |

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
    → allowed: IDLE, UNKNOWN, WAITING_INPUT, PAGINATED, COMPLETED, TIMED_OUT, STUCK
    → rejected: EXECUTING, LONG_RUNNING
  → None (ok) → write data
  → TerminalGuardResult → return diagnostic to LLM
```

Key difference: ProcessTool allows `WAITING_INPUT` (for typing passwords) and `PAGINATED` (for sending 'q'/Space), while CommandTool rejects both (a new command would corrupt the interaction). ProcessTool also allows `STUCK` — unrecognized silent prompts are its most common STUCK cause, and the STUCK suggestion text tells the agent to `process write`; CommandTool keeps rejecting it (a new command into a possibly-hung terminal is harmful).

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

### Async-safety contract (ADR-0032)

`TerminalBackend` (ABC) enforces a structural async-safety contract so that
no backend's `write` / `read_pending` blocks the event loop. Three I/O
shapes are supported:

1. **Blocking-IO hooks** (byte-stream backends: `WinptyHiddenBackend`,
   `PexpectPtyBackend`) — implement `_write_blocking` / `_read_blocking`
   hooks; the base-class `write` / `read_pending` template methods wrap them
   in `loop.run_in_executor(None, …)`. The hooks are opt-in (default
   `raise NotImplementedError`).

2. **Native async** (byte-stream backend: `WinptyConsoleWindowBackend`) —
   overrides `write` / `read_pending` directly with `asyncio.StreamWriter` /
   `StreamReader`. No hooks. Used when the underlying transport is already
   `await`-shaped (asyncio streams after ADR-0032 D2).

3. **Snapshot backend** (`TmuxPtyBackend`) — overrides `write` /
   `read_pending` / `current_segment` / `drain_startup` directly because
   tmux's I/O model is a control-protocol snapshot (`send_keys` writes,
   `capture_pane` reads a pane snapshot), not a byte stream. No hooks.
   ADR-0032 D5 explicitly rejects forcing tmux into byte-stream shape via
   `pipe-pane` as over-convergence.

Every concrete backend implements `_shell_family() -> ShellFamily`
(`@abstractmethod` on the base). The base class uses it to gate
readline-dependent behaviors: `clear_input_line()` sends `\x01\x0b` iff
`_shell_family().uses_readline()`; `drain_startup()` passes
`uses_readline=_shell_family().uses_readline()` to the shared
`drain_windows_startup` helper. This replaces the three divergent
shell-detection heuristics that existed pre-ADR-0032 (fragile substring
`"bash" in self._shell`, `_family_from_path`, inline suffix tuple).

The three byte-stream backends inherit `current_segment` /
`clear_input_line` / `drain_startup` from the base class; tmux overrides
`current_segment` and `drain_startup` (snapshot I/O requires
`capture_pane`-based prompt detection).

Architecture guard: `tests/architecture/test_terminal_backend_contract.py`
asserts the contract shape; `tests/architecture/test_terminal_async_safety.py`
(ticket 07) asserts per-subclass compliance.

### Windows: WinptyConsoleWindowBackend (visible)

Launches `visible_windows_host.py` in a new OS console window. Per
ADR-0032 D2, the parent↔host IPC bridge uses `asyncio.start_server` /
`asyncio.open_connection` + `StreamReader` / `StreamWriter` (was: raw
`socket.socket` + `settimeout` + `sendall` / `recv`). This structurally
eliminates the `settimeout` leak and partial-`sendall` defects that
produced the "tab stuck" and "command typed but not submitted" symptoms.

- Human can see and type in the visible window
- Agent commands also appear in the same window
- Parent↔host IPC: asyncio streams with `TCP_NODELAY` set on both sides
- Host process: blocking pywinpty calls wrapped in `run_in_executor`;
  `_stdin_to_pty` (human keyboard) and `_resize_monitor` remain threads
- Uses `winpty.Backend.WinPTY` explicitly (avoids ConPTY DA1 pollution)
- Human Ctrl+C is handled by `SetConsoleCtrlHandler` (host survives)
- `isalive()` check ensures host exits when shell dies

Data flow:

```
agent parent -> asyncio Stream -> visible host -> pywinpty -> bash
human keyboard -> visible host -> pywinpty -> bash
bash output -> pywinpty -> visible host -> asyncio Stream -> agent
bash output -> pywinpty -> visible host -> stdout (visible window)
```

### Windows: WinptyHiddenBackend (hidden)

In-process pywinpty, no visible window. Implements `_write_blocking` /
`_read_blocking` hooks (ADR-0032 D1/D3); the base-class template wraps
them in `run_in_executor`, eliminating the synchronous-write-blocks-event-
loop defect.

### Unix: PexpectPtyBackend (primary, hidden) + TmuxPtyBackend (fallback, both)

Linux uses a degradation chain: `PexpectPtyBackend` (native PTY via
pexpect) → `TmuxPtyBackend` (tmux sessions). `create_pty_backend()` checks
pexpect availability first; if unavailable, falls back to tmux. The single
`BaseTerminalManager` (two-axis `shell_info` × `visibility`) auto-detects
available backends through the factory.

`PexpectPtyBackend` implements `_write_blocking` / `_read_blocking` hooks
(ADR-0032 D1/D3).

`TmuxPtyBackend` is a **snapshot backend** (ADR-0032 D5):
`capture-pane -p -S -` (full scrollback) + prefix-match diff (no
duplicates on >30-line commands); `is_alive` has a 1-second TTL cache to
avoid spawning `tmux ls` ~20×/s under the poll loop.

Legacy aliases `VisibleWindowsPtyBackend` / `WindowsHiddenPtyBackend` are
re-exported in `backends/__init__.py` for the migration window.

### Fallback: PersistentBashTool

When bash is unavailable or `use_terminal=false`, the bot falls back to
`PersistentBashTool` — one persistent interactive bash per conversation
(stateful cwd/env/backgrounds, routed by session_id) plus its `bash_input`
companion.

---

## Bot Project Integration

`examples/bot_project/config/bot_config.yml`:

```yaml
main:
  use_terminal: true   # Enable CommandTool/ProcessTool/TerminalTool

terminal:
  close_on_exit: false  # Keep tabs open after bot shutdown
```

`examples/bot_project/bot/service/react_strategy.py` (`ReactExecutionStrategy.assemble_main`):
- Creates the pool's terminal manager via the framework factory
  `create_terminal_manager_or_none()` (shell detection is framework
  ladder logic; returns `None` when `use_terminal=false` or no backend
  is available)

Bash/process/terminal tools are not hand-registered in bot code: the
compiled roster's `bash` / `process` / `terminal` entries resolve through
the FW TOOL-slot factories (`modex_agent/plugins/defaults/tools.py`) —
`CommandTool` bound to the pool terminal manager when one exists, else the
pool's `PersistentBashTool` fallback (with the `bash_input` companion
ensured by `native_core.assemble_native_agent`) — the same single road for
main agents and subagents.

---

## Key Behaviors and Constraints

1. **Bash-only on Windows**: Bot project enforces WSL bash or Git Bash via `detect_platform_shell()`. CMD and PowerShell are not supported as terminal shells; if no bash is available the pool falls back to `PersistentBashTool` (POSIX pty — errors on Windows at first use).
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
| `persistent_bash.py` | `PersistentBashTool` + `BashInputTool` — stateless routing shells over the pool-level `PersistentShellManager` (one shell per conversation session_id, `_current_session_id` contextvar routing — the `BaseTerminalManager` shape applied to the pair; `__default__` shell when no routing context). `^C`/`ctrl+c`/`\x03` translate to the SIGINT byte (terminal-trio convention); under terminal takeover (ADR-0045) the `^C` byte is forwarded verbatim to the program that owns the terminal |
| `_persistent_session.py` | `PersistentShellManager` (per-conversation shell registry: lazy materialization, LRU touch, over-limit reap, `close_all`) + `PersistentShellSession`: the pexpect driver behind the persistent bash tools, detecting interactive state from kernel terminal facts (ADR-0045). Paired START/END printf marker protocol (output sliced to the command's own pair; foreign marker lines stripped) with the END marker as the absolute first completion signal; per-session call lock (same-session serialization, cross-session parallelism); `_Phase{IDLE,RUNNING,WAITING}` guard with WAITING kind classified from the kernel signal after a Linux `/proc` probe hit (shell-kind passes bash through; prompt-kind keeps the guard; stale waits self-heal, misclassified ones reclassify). The single probe `_terminal_state()` reads the termios ICANON bit and the foreground process group (`tcgetpgrp` vs the shell's), classifying four states (`SHELL_READLINE`/`SHELL_CANONICAL`/`CHILD_RAW`/`CHILD_CANONICAL`): the PS1-token abnormal-completion layer fires only on `SHELL_READLINE` (or probe absence); `CHILD_RAW` opens the interactive-takeover exit (quiet 0.25s + two consecutive 25ms polls + non-empty buffer) returning partial output with an interactive-shell `[hint: ...]` advisory while keeping the transaction answerable (WAITING, pending preserved, process alive); keyword and weak prompt-shape layers stay as fallbacks for states the kernel matrix structurally cannot see (canonical prompts; builtin reads where the shell owns the foreground), quiet-window gated. Silence is never settlement: a silent foreground command waits for its marker or the deadline. Session-wide SIGKILL on timeout/cancel; deadline (480s) sits strictly below the executor default (540s) so the graceful timeout path is reachable first |
| `subprocess_tool.py` | Stateless bash execution (fresh process per call) — retained for direct callers/tests, no longer wired into builders |
| `_foreground_probe.py` | Linux `/proc` stdin-wait evidence (tpgid foreground group + per-thread syscall scan: read on a tty-backed fd — ANY fd, covering ssh/sudo `/dev/tty` password reads — / select / poll / epoll watching fd 0) — injectable internals for tests |
| `guard.py` | `TerminalGuard` — pre-flight input validation. `check_command_writable()` (CommandTool) and `check_process_writable()` (ProcessTool) enforce status-based allowlists; returns `TerminalGuardResult` with diagnostic `TerminalSnapshot` |
| `poll_loop.py` | Shared `poll_until_settled()` — reused by CommandTool and ProcessTool for post-write drain. `PollOutcome` enum (PROMPT_DETECTED / YIELDED / TIMED_OUT / INPUT_WAIT / STUCK / LONG_RUNNING / PROCESS_EXIT / PAGINATED) |
| `env.py` | `build_full_env()` — complete environment dict for child processes. On Windows, merges missing HKLM/HKCU PATH entries from registry |
| `prompt.py` | Prompt detection, ANSI/DA1 stripping, pager detection, startup drain |
| `types.py` | `ShellFamily`, `ShellInfo`, `Platform`, `detect_platform_shell` |
| `config.py` | Terminal configuration |
| `state_store.py` | Terminal state persistence |
| `results.py` | Result types |
| `pty_keys.py` | PTY key constants |
| `backends/base.py` | `TerminalBackend` ABC |
| `backends/factory.py` | `create_pty_backend()` — platform-auto backend selection |
| `backends/visible_windows.py` | Visible Windows backend (winpty) — parent side |
| `backends/visible_windows_host.py` | Visible console helper process |
| `backends/windows_hidden.py` | `WinptyHiddenBackend` (legacy alias `WindowsHiddenPtyBackend`) — hidden Windows PTY backend |
| `backends/pexpect_pty.py` | `PexpectPtyBackend` — Linux native PTY via pexpect |
| `backends/tmux_pty.py` | `TmuxPtyBackend` — Unix tmux backend |
