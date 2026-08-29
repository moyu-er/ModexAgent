<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-25 -->

# Terminal System — Agent Guide

## Overview

The terminal system provides **stateful, persistent shell sessions** for agents.
Unlike stateless subprocess execution (where each command runs in isolation),
this system keeps a real terminal process alive across tool calls, preserving:

- `cd` state and working directory
- Environment variables and aliases
- SSH connections and interactive sessions
- Partial output when the command deadline hits

Each session is backed by an OS-visible terminal window when available,
allowing human users to observe or intervene in the same session.

The agent-facing surface is a trio of tools (ADR-0044):

- `bash` (CommandTool) — run a command in the selected tab; returns exactly
  `completed` / `waiting_input` (advisory) / `timed_out` / `rejected`
- `process` (ProcessTool) — send one input line to the running command, or
  interrupt it with `^C`; process results may return `waiting_input` when
  another prompt appears and `timed_out` when the refreshed deadline closes
  the tab
- `terminal` (TerminalTool) — manage tabs (open / close / list / select)

---

## Architecture: Three Layers

```
+-------------------+     +-------------------+     +-------------------+
|  CommandTool      | --> |  TerminalSession  | --> |  TerminalBackend  |
|  (agent-facing)   |     |  (session logic)  |     |  (OS process)     |
+-------------------+     +-------------------+     +-------------------+
         |                         |                         |
         |   execute("cmd")        |   poll/read            |   PTY/tmux
         |                         |                         |
         v                         v                         v
  <command_result> XML     prompt/state detection      bash/cmd visible
```

### Layer 1: CommandTool (`modex_agent/tools/terminal/command_tool.py`)

The **agent-facing** shell execution tool (`bash`) for persistent terminal
sessions. It submits the command through `TerminalSession`, settles it with
the shared poll loop, and records it in the pool's `ProcessRegistry`.

Every result is `<command_result>` XML with exactly four statuses:

| Status | Meaning |
|--------|---------|
| `completed` | Prompt-stability or process-exit evidence proved the command finished. Output included. |
| `waiting_input` | **Advisory**: ≥ `input_wait_idle_ms` (10 s) of output silence PLUS positive stdin-wait evidence (see Poll Loop). The wording is soft on purpose: the command MAY be waiting for input (a prompt, password request, pager) or may simply be slow. The agent judges from the output: answer it with the process tool, interrupt with `^C` via process, or keep waiting. The command keeps running under the 480 s deadline. |
| `timed_out` | The 480 s command deadline expired. Partial output included; the tab was closed and the shell reset (working directory and environment variables are NOT preserved). |
| `rejected` | The input guard refused submission: a command is still running or waiting in the tab. Interact with it via the process tool first. |

There is **no `^C` path in bash**: interrupting a running command is the
process tool's job.

When the pool has no terminal manager (`use_terminal=false`, or every backend
is unavailable on this platform), the `bash` slot degrades to
`PersistentBashTool` — one persistent interactive bash per conversation
(stateful cwd/env, routed by the caller's session_id) — plus its
`BashInputTool` companion on POSIX-pty hosts, or to the stateless
`SubprocessTool` on hosts without a POSIX pty (see Bot Project Integration).

### Layer 1b: PersistentBashTool (fallback)

Registered when no terminal backend is available (e.g. subagents or when
`use_terminal=false`). One stateful bash per conversation (per-session_id
routing via `_current_session_id`; `__default__` shell without a routing
context); `bash_input` answers commands that block reading stdin.
POSIX-only (pexpect).

### Layer 2: TerminalSession (`modex_agent/tools/terminal/session.py`)

The **per-tab execution engine**. Each session wraps one backend and handles:

1. **Lazy startup** — the backend starts on first use (`ensure_started`);
   `TerminalTool.open` also starts it eagerly so visible windows appear at once
2. **Startup drain** — consume banner/prompt on newly started terminals; zsh
   additionally gets a PS1 override (oh-my-zsh / Powerlevel10k glyphs defeat
   prompt detection)
3. **Command submission** — `submit_command()` discards pending output, seals
   the previous command's block in the sliding buffer, and writes the command
   with the shell-appropriate line ending (`\r` for readline shells)
4. **Polling reads** — `poll_once()` strips DECCKM (smkx/rmkx) and
   bracketed-paste sequences, auto-answers DSR cursor-position queries, and
   tracks raw-byte activity for idle measurement
5. **State events** — `apply_outcome(result)` is the single state-event entry
   point (ADR-0010 Decision 7): `PROMPT_DETECTED` / `PROCESS_EXIT` clear
   `_command_started_at`; `INPUT_WAIT` / `TIMED_OUT` keep it (the interaction
   is still live)
6. **Status derivation** — `command_status()` computes the guard-facing status
   (priority: COMPLETED > UNKNOWN > WAITING_INPUT > IDLE > EXECUTING)
7. **Output extraction** — `last_command_output()` slices the last command's
   block out of the backend buffer; model-facing output is sanitized at the
   tool layer

Special states (`TerminalCommandStatus`, computed by `command_status()`):

| Status | Meaning | Next action |
|--------|---------|-------------|
| `completed` | Backend died — the command finished | New bash commands allowed |
| `unknown` | No bytes ever received (fresh-tab safety net) | New bash commands allowed |
| `waiting_input` | Output looks like an input prompt, or a command has run silently ≥ 10 s | Answer via the process tool (bash rejects new commands) |
| `idle` | Clean empty prompt | New bash commands allowed |
| `executing` | A command is running and has produced output | Bash rejects; process write rejected unless the process has been silent ≥ 1 s |

The vocabulary also carries `timed_out` (allowed by both guard allowlists),
but deadline expiry surfaces as a registry `ProcessStatus.TIMED_OUT` plus tab
closure, not through `command_status()`.

### Layer 3: BaseTerminalManager (`modex_agent/tools/terminal/managers.py`)

The **session registry** owning all named tabs (ADR-0010 two-axis: `shell_info`
× `visibility`). The legacy OS-named managers and the second `TerminalManager`
class have been folded inward into this single implementation; capability
behaviours (LRU, memory-pressure buffer clearing) are flag-guarded private
methods. Memory-pressure defaults to on (global 1M cross-tab ceiling); LRU
remains opt-in:

- **Named collection** — `name -> TerminalSession` map
- **Default terminal** — the tab `bash` and `process` use when no name is
  specified; `get_default()` creates a fresh `"default"` tab if none exists
  or the current one died
- **LRU eviction** (opt-in, `max_terminals`) — closes the least-recently-used
  session when the cap is exceeded
- **Lazy alive detection** — a dead default tab is dropped on use, not on idle
- **Memory pressure** (default on) — clears the largest non-default buffers
  when the cross-tab total exceeds `max_total_buffer_chars`

Key methods:
- `get_or_create(name)` — get existing or create new, with LRU eviction
- `get_default_session()` / `get_default()` — default tab (None vs auto-create)
- `select_default(name)` — switch default tab
- `list_sessions()` — metadata for all tabs; `TerminalTool list` filters dead
  tabs out so the agent only sees active ones
- `close(name)` — terminate a session; **closing the default tab reselects
  another live tab** (or the next `get_default()` creates a fresh one)
- `create_terminal_manager_or_none(...)` — pool factory with the platform
  fallback ladder (shell detection → requested visibility → fallback
  visibility → None)

### Layer 4: TerminalTool (`modex_agent/tools/terminal/tool.py`)

The **agent-facing tab management tool**. Registered only when a terminal
manager is available.

Actions:

| Action | Purpose | Required params |
|--------|---------|-----------------|
| `open` | Create a new named tab AND select it (starts at the workspace directory) | `name` (optional, auto-generated) |
| `close` | Terminate a tab (its shell dies; default reselects) | `name` |
| `list` | Show live tabs — `(default)` marks the selected one, with its running command | — |
| `select` | Switch the default tab | `name` |

### Command deadline & watchdog (ADR-0044)

Every command gets a **480 s budget** (`command_deadline_seconds` in
`config.py`): `ProcessRegistry.create()` stamps
`deadline_at = now + 480 s` on each `ProcessSession`.

- **In-flight expiry** — the poll loop returns `TIMED_OUT`; CommandTool marks
  the process `TIMED_OUT`, returns partial output plus the reset notice, and
  closes the tab via `manager.close()` (closing the default tab reselects).
  The next bash call lands on a fresh tab whose result carries the hint
  "Previous tab timed out after 480s and was closed."
- **Post-advisory expiry** — if bash or process already returned a
  `waiting_input` advisory, the pool-scoped `TerminalWatchdog` (`watchdog.py`)
  closes the tab at the deadline before marking the process `TIMED_OUT`; close
  failures leave it RUNNING for the next scan to retry. `PoolAssembleStage`
  registers `watchdog.stop` BOTH on the assembly builder (failure-path
  cleanup — `AssemblyPipeline` runs `builder.cleanup()` when a later stage
  fails) AND on `AgentPool.attach_background_stop` (executed by
  `AgentPool.shutdown_all()` at pool shutdown); `stop` is idempotent, so a
  failed assembly whose pool is also torn down double-stops safely.
- **Deadline refresh** — every successful process write and every `^C`
  interrupt call `ProcessRegistry.refresh_deadline()`, restarting the 480 s
  clock for the interaction.
- **Why 480** — the deadline sits strictly below the executor's 540 s tool
  timeout (`TOOL_TIMEOUT_SECONDS`, `core/constants.py`), so the graceful
  close-tab-and-reset path always fires before the executor kills the call.

---

## Input Guard (guard.py)

Before CommandTool or ProcessTool sends data to the terminal, a guard checks
the session's derived status:

```
CommandTool.execute("ls")
  → check_command_writable(session)
    → allowed: IDLE, UNKNOWN, COMPLETED, TIMED_OUT
    → rejected: EXECUTING, WAITING_INPUT
  → None (ok) → proceed with command
  → TerminalGuardResult → <status>rejected</status> + message + suggestion

ProcessTool.execute(data=..., submit=...)
  → check_process_writable(session, registry)
    → allowed: IDLE, UNKNOWN, COMPLETED, TIMED_OUT, WAITING_INPUT
    → rejected: EXECUTING (with one exception, below)
  → None (ok) → write data
  → TerminalGuardResult → <status>rejected</status> + message + suggestion
```

Key difference: ProcessTool additionally allows `WAITING_INPUT` — answering
prompts is its job; a new bash command would corrupt the interaction. Two
suggestion texts:

- EXECUTING: "A command is still running in this tab. Use process (^C to
  interrupt, or send input), or wait."
- WAITING_INPUT: "The previous command may be waiting for input. Answer it
  with the process tool first."

**Silent-consumer exception**: the process guard lets EXECUTING through when
the running process has produced no output for ≥ 1 s. Silent stdin consumers
like `cat > file` never print a prompt but are waiting for input; EXECUTING
with prior output (e.g. build output) is still rejected so data is never
injected into a command that isn't expecting it.

---

## Poll Loop (poll_loop.py)

Both `CommandTool.execute()` and ProcessTool's post-write drain share one
poll loop:

```
poll_until_settled(session, registry, proc_id, config, check_input_wait=...)
  → poll cycle:
     1. Read pending output (registry accumulates it; idle timer resets)
     2. Backend dead?                                   → PROCESS_EXIT
     3. Prompt stable for prompt_stabilize_ms (100 ms)?  → PROMPT_DETECTED
     4. Quiet ≥ input_wait_idle_ms (10 s) AND evidence?  → INPUT_WAIT
     5. deadline_at reached?                            → TIMED_OUT
```

Four outcomes (`PollOutcome`): `prompt_detected` / `process_exit` /
`input_wait` / `timed_out`.

**Evidence fusion** (step 4): quiet alone is never a verdict. The advisory
fires only when the ≥ 10 s quiet window coincides with at least one positive
signal, OR-fused:

- **Kernel probe** — `backend.stdin_wait_evidence()`: is the foreground
  process group blocked reading stdin? Implemented by `PexpectPtyBackend`
  via the Linux `/proc` probe (`_foreground_probe.py`); the base backend and
  non-Linux transports return `None` (no evidence)
- **Content markers** — `is_waiting_for_input()`: password / passphrase /
  `[y/n]` / confirm keywords, or a prompt-shaped line ending (`:`, `?`, `]`,
  `)`) that isn't a shell prompt
- **Pager patterns** — `detect_pager_entry()`: `less` (`:`, `(END)`, status
  line) and `more` (`--More--`) cursor shapes

A command that is merely slow (a long `grep`, a build) stays quiet, produces
no evidence, and keeps polling until it finishes or the 480 s deadline closes
the tab. `mark_exited_if_finished()` maps only `PROMPT_DETECTED` /
`PROCESS_EXIT` to registry completion — every other outcome leaves the
interaction live.

---

## How the Tools Work Together

### Normal Flow

```
Agent calls bash.execute("ls -la")
  -> manager.get_default()            [creates "default" tab if none or dead]
  -> check_command_writable(session)  [guard: status must be writable]
  -> session.ensure_started(env=...)  [lazy backend start + startup drain]
  -> registry.create(...)             [ProcessSession, deadline_at = +480 s]
  -> session.submit_command("ls -la") [discard pending, seal buffer, write + \r]
  -> poll_until_settled(...)          [shared poll loop]
  -> session.apply_outcome(result)    [single state-event entry point]
  -> <command_result> XML (completed / waiting_input / timed_out)
```

### Command Deadline Flow

```
Agent calls bash.execute("sleep 600")
  -> no prompt, no stdin-wait evidence → quiet is not a verdict → keeps polling
  -> 480 s deadline expires → TIMED_OUT
  -> registry marks the process TIMED_OUT; manager.close(tab) — default reselects
  -> returns <status>timed_out</status> + partial output + reset notice
     (working directory and environment variables are NOT preserved)

Agent calls bash.execute("echo next")
  -> get_default() creates a fresh tab
  -> result hint: "New terminal tab 'default' created. Previous tab timed out
     after 480s and was closed."
```

If the deadline expires while bash has ALREADY returned an advisory, the
TerminalWatchdog's 5 s scan closes the tab in the background — the next bash
call lands on a fresh tab with the same hint.

### SSH / Interactive Flow

```
Agent calls bash.execute("ssh root@host")
  -> "root@host's password:" prompt → 10 s quiet + content marker → INPUT_WAIT
  -> returns <status>waiting_input</status> advisory (the command keeps running)

Agent judges from the output (for a password: STOP and ask the user first)
  -> process.execute(data="<answer>", submit=true)  [guard allows WAITING_INPUT]
     or process.execute(data="^C")                  [abort the command]
  -> write + shared drain → continued output (completed when the prompt returns)
```

### Multi-Tab Flow

```
Agent calls terminal.execute(action="open", name="remote")   # create AND select
Agent calls bash.execute("ssh user@server")                  # runs in "remote"
Agent calls terminal.execute(action="open", name="local")    # new tab, now default
Agent calls bash.execute("make")                             # runs in "local"
Agent calls terminal.execute(action="select", name="remote") # switch back
Agent calls process.execute(data="^C")                       # interrupt in "remote"
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
produced the "hung tab" and "command typed but not submitted" symptoms.

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
(ADR-0032 D1/D3) and the `stdin_wait_evidence()` Linux kernel probe
consumed by the poll loop's input-wait detection.

`TmuxPtyBackend` is a **snapshot backend** (ADR-0032 D5):
`capture-pane -p -S -` (full scrollback) + prefix-match diff (no
duplicates on >30-line commands); `is_alive` has a 1-second TTL cache to
avoid spawning `tmux ls` ~20×/s under the poll loop.

Legacy aliases `VisibleWindowsPtyBackend` / `WindowsHiddenPtyBackend` are
re-exported in `backends/__init__.py` for the migration window.

### Fallback: PersistentBashTool

When no terminal manager exists (`use_terminal=false` or every backend
unavailable), the `bash` slot falls back to `PersistentBashTool` — one
persistent interactive bash per conversation (stateful cwd/env/backgrounds,
routed by session_id) plus its `bash_input` companion; hosts without a POSIX
pty get the stateless `SubprocessTool` instead.

---

## Bot Project Integration

Terminal wiring is declaration- and roster-driven (no hand registration in
bot service code):

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
- `PoolAssembleStage` enforces the manager↔registry invariant and wires the
  `TerminalWatchdog` for every pool with a terminal manager.

---

## Key Behaviors and Constraints

1. **Windows shell ladder**: `detect_platform_shell()` prefers Git Bash →
   WSL bash → PowerShell → cmd. Without a terminal manager the `bash` slot
   degrades to `PersistentBashTool` (POSIX-pty hosts) or stateless
   `SubprocessTool` (`plugins/defaults/tools.py`).
2. **Eager startup on open**: `TerminalTool.open` calls `ensure_started()`
   so visible windows appear immediately — no need to wait for the first
   command.
3. **`list` shows live tabs only**: dead tabs are filtered out of
   `TerminalTool list`; because `open` eagerly starts the backend, a freshly
   opened tab is alive from the start.
4. **CRLF normalization**: Windows console auto-expands `\n` to `\r\n`.
   PTY output with `\r\n` is normalized to `\n` before stdout write to prevent
   blank lines (`\r\r\n`).
5. **Output sanitization**: Model-facing output strips ANSI/CSI/DA1/OSC
   control sequences. Visible terminal output is NOT modified.
6. **Interrupts route through the process tool**: `process` with
   `data="^C"` (or `ctrl+c` / `\x03`) sends a real Ctrl+C to the running
   command and refreshes the command deadline; `bash` has no `^C` path.

---

## Files

| File | Purpose |
|------|---------|
| `manager.py` | Deprecated alias — `TerminalManager = BaseTerminalManager` (re-exported for the migration window) |
| `managers.py` | `BaseTerminalManager` (single two-axis impl: shell_info × visibility, with folded flag-guarded LRU / persistence / memory-pressure) + `create_terminal_manager` / `create_terminal_manager_or_none` factories |
| `session.py` | Per-tab execution engine — lazy startup, startup drain, `submit_command`, `poll_once` (DECCKM/DSR/bracketed-paste stripping), `command_status`, `apply_outcome` |
| `tool.py` | LLM tab management tool (open / close / list / select) |
| `process_tool.py` | Write-only input tool for the running command — one `data` line (`submit` owns the Enter), routes `^C`/`ctrl+c`/`\x03` to a real Ctrl+C |
| `process_registry.py` | Process tracking — `ProcessSession` records (deadline, idle, capped output), running/finished maps, `refresh_deadline` |
| `command_tool.py` | Command execution tool — input guard, shared poll loop, `<command_result>` XML (completed / waiting_input / timed_out / rejected) |
| `persistent_bash.py` | `PersistentBashTool` + `BashInputTool` — stateless routing shells over the pool-level `PersistentShellManager` (one shell per conversation session_id, `_current_session_id` contextvar routing — the `BaseTerminalManager` shape applied to the pair; `__default__` shell when no routing context). `^C`/`ctrl+c`/`\x03` translate to the SIGINT byte (terminal-trio convention); under terminal takeover (ADR-0045) the `^C` byte is forwarded verbatim to the program that owns the terminal |
| `_persistent_session.py` | `PersistentShellManager` (per-conversation shell registry: lazy materialization, LRU touch, over-limit reap, `close_all`) + `PersistentShellSession`: the pexpect driver behind the persistent bash tools, detecting interactive state from kernel terminal facts (ADR-0045). Paired START/END printf marker protocol (output sliced to the command's own pair; foreign marker lines stripped) with the END marker as the absolute first completion signal; per-session call lock (same-session serialization, cross-session parallelism); `_Phase{IDLE,RUNNING,WAITING}` guard with WAITING kind classified from the kernel signal after a Linux `/proc` probe hit (shell-kind passes bash through; prompt-kind keeps the guard; stale waits self-heal, misclassified ones reclassify). The single probe `_terminal_state()` reads the termios ICANON bit and the foreground process group (`tcgetpgrp` vs the shell's), classifying four states (`SHELL_READLINE`/`SHELL_CANONICAL`/`CHILD_RAW`/`CHILD_CANONICAL`): the PS1-token abnormal-completion layer fires only on `SHELL_READLINE` (or probe absence); `CHILD_RAW` opens the interactive-takeover exit returning partial output with an interactive-shell `[hint: ...]` advisory while keeping the transaction answerable (WAITING, pending preserved, process alive); keyword and weak prompt-shape layers stay as fallbacks for states the kernel matrix structurally cannot see (canonical prompts; builtin reads where the shell owns the foreground), quiet-window gated. Silence is never settlement: a silent foreground command waits for its marker or the deadline. Session-wide SIGKILL on timeout/cancel; deadline (480s) sits strictly below the executor default (540s) so the graceful timeout path is reachable first. Empty-evidence gate: zero real output (START marker alone) never settles a mode/shape verdict; the Linux probe is the sole zero-output authority; takeover needs 0.75s quiet + 3 raw-child samples + command-owned output; probe-less weak-shape needs a 2.0s window |
| `subprocess_tool.py` | Stateless bash execution (fresh process per call) — the `bash`-slot fallback on hosts without a POSIX pty; also usable for direct callers/tests |
| `_foreground_probe.py` | Linux `/proc` stdin-wait evidence (tpgid foreground group + per-thread syscall scan), injectable internals for tests. Definitional on both axes: the read/poll target must be the session's own controlling terminal (tty device-number match on ANY fd, `/dev/tty` alias accepted, covering ssh/sudo password reads), and select/poll/epoll waits count only when indefinite (NULL timeout pointer or -1 timeout); bounded-timeout pollers (key checks, progress bars, event-loop ticks) are running commands, not input waits |
| `guard.py` | Pre-flight input validation — `check_command_writable()` (CommandTool: IDLE/UNKNOWN/COMPLETED/TIMED_OUT) and `check_process_writable()` (ProcessTool: + WAITING_INPUT, silent-EXECUTING exception); returns `TerminalGuardResult` with diagnostic `TerminalSnapshot` |
| `poll_loop.py` | Shared `poll_until_settled()` — reused by CommandTool and ProcessTool for post-write drain. `PollOutcome` enum (prompt_detected / process_exit / input_wait / timed_out); evidence-fused input-wait detection; `mark_exited_if_finished` |
| `watchdog.py` | `TerminalWatchdog` — pool-scoped 5 s scanner closing tabs whose command deadline expired; wired in `PoolAssembleStage`, stopped via builder cleanup (assembly failure) and `AgentPool.shutdown_all` (pool shutdown) |
| `env.py` | `build_full_env()` — complete environment dict for child processes. On Windows, merges missing HKLM/HKCU PATH entries from registry |
| `prompt.py` | Prompt detection, ANSI/DA1 stripping, pager detection, startup drain |
| `types.py` | `ShellFamily`, `ShellInfo`, `Platform`, `detect_platform_shell`, status enums (`CommandResultStatus`, `TerminalCommandStatus`, `ProcessStatus`), terminal XML truncation metadata |
| `config.py` | Terminal runtime config (7 fields): `command_deadline_seconds` (480), `input_wait_idle_ms`, `max_output_chars`, `pending_max_output_chars`, `finished_ttl_ms`, `prompt_stabilize_ms`, `max_total_buffer_chars` |
| `results.py` | Result types |
| `pty_keys.py` | PTY key constants + `normalize_write_payload` (`submit`-owned Enter semantics for process writes) |
| `backends/base.py` | `TerminalBackend` ABC |
| `backends/factory.py` | `create_pty_backend()` — platform-auto backend selection |
| `backends/visible_windows.py` | Visible Windows backend (winpty) — parent side |
| `backends/visible_windows_host.py` | Visible console helper process |
| `backends/windows_hidden.py` | `WinptyHiddenBackend` (legacy alias `WindowsHiddenPtyBackend`) — hidden Windows PTY backend |
| `backends/pexpect_pty.py` | `PexpectPtyBackend` — Linux native PTY via pexpect |
| `backends/tmux_pty.py` | `TmuxPtyBackend` — Unix tmux backend |
