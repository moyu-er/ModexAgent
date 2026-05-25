# Terminal Command Redesign Specification

**Date:** 2026-05-25
**Status:** Draft for review
**Scope:** Windows-first terminal command redesign, replacing the current `ShellTool`/subprocess execution path.

## Goal

Replace the current weak interactive shell execution model with an OpenClaw-inspired command/session/process model. The new design must support both user-visible terminal tabs and hidden PTY tabs while keeping that distinction invisible to tools and agents. First implementation targets Windows only; macOS/Linux and additional shells are explicit extension points, not first-phase requirements.

## Non-Goals

- Do not support every OS and shell in the first implementation.
- Do not keep subprocess as a fallback execution path.
- Do not expose `visible` or `hidden` as a tool parameter the agent must reason about.
- Do not make the command tool infer and automatically answer interactive prompts.

## Design Principles

1. **Tool semantics are uniform.** `command`, `process`, and `terminal` tools operate on terminal/session concepts. They do not know whether a tab is visible or hidden.
2. **Visibility is a manager/backend decision.** A configured `TerminalManager` implementation decides whether tabs are visible Windows consoles or hidden PTY sessions.
3. **OS and shell are layered concerns.** The design must separate OS platform, shell family, manager type, and backend implementation so later macOS/Linux and bash/cmd/sh support can be added without changing tool APIs.
4. **Process follow-up is explicit.** Long-running or interactive commands return a `session_id`; follow-up interaction uses the `process` tool.
5. **No prompt-only completion model.** Process exit state is authoritative. Prompt detection and terminal-screen heuristics are auxiliary diagnostics, especially for persistent shell tabs.
6. **The active terminal may stop matching the launch shell.** After `ssh`, `sudo -i`, `cmd`, `powershell`, `bash`, REPLs, text UIs, or nested shells, the original local `ShellInfo` is only launch metadata. I/O correctness must rely on the PTY byte stream, process/session state, stdin writability, idle timing, and explicit follow-up actions.

## Reference Model From OpenClaw

OpenClaw separates starting commands from managing running command sessions:

- `exec` starts a command. It may finish in the foreground or yield/background and return a `sessionId`.
- `process` manages already-started sessions with `list`, `poll`, `log`, `write`, `send-keys`, `submit`, `paste`, `kill`, `clear`, and `remove`.
- Running sessions track pending output separately from aggregated historical output.
- `poll` drains pending output and does not repeat old content.
- `log` reads aggregated historical output with slicing/paging.
- `waitingForInput` is a hint computed from runtime state: stdin is writable and no new output has arrived for a configured idle threshold, default 15 seconds.
- Killing first cancels the managed session and then falls back to process-tree termination.

This design adapts those behaviors into Python and the existing ModexAgent tool/runtime architecture.

## Public Tool Surface

### `command`

Replaces the current `shell` tool.

Purpose: start a command in a named terminal/session and return either a completed result or a running session handle.

Parameters:

```python
{
    "command": "string, required",
    "terminal": "string, optional named tab/session; default is manager default",
    "workdir": "string, optional working directory",
    "env": "object[str, str], optional environment overrides",
    "timeout": "number, optional hard timeout in seconds",
    "yield_ms": "number, optional foreground wait window before returning running",
    "background": "boolean, optional; true returns running immediately",
    "pty": "boolean, optional; defaults true for first implementation",
}
```

Result details:

```python
{
    "status": "completed | running | failed | killed | timed_out",
    "session_id": "string | None",
    "terminal": "string",
    "pid": "int | None",
    "cwd": "string | None",
    "exit_code": "int | None",
    "exit_signal": "string | int | None",
    "timed_out": "bool",
    "duration_ms": "int | None",
    "failure_kind": "string | None",
    "message": "string | None",
    "started_at": "float | None",
    "ended_at": "float | None",
    "output": "string",
    "tail": "string",
    "truncated": "bool",
    "stdin_writable": "bool | None",
    "waiting_for_input": "bool | None",
    "idle_ms": "int | None",
}
```

Behavior:

- If the command exits before `yield_ms` or the default foreground window, return `completed` or `failed`.
- If `background=true` or the foreground window expires while the process is still running, return `running` with `session_id`.
- If `timeout` expires, terminate the process tree and return `timed_out` with timeout context and the terminal output captured up to the timeout. Do not collapse this to a generic error string.
- If no output arrives for the input idle threshold while stdin is writable, return a running result with `waiting_for_input=true`; the model must decide whether to call `process write`, `process submit`, `process send_keys`, or wait/poll again.
- `yield_ms` and `timeout` are different controls. `yield_ms` controls how long the tool waits before returning a running session; it does not kill the command. `timeout` controls the hard maximum runtime; it kills the command/session when exceeded.

Default resolution should follow OpenClaw's pattern: parameters are optional at the tool boundary, then normalized through manager/tool defaults with min/max clamps. Recommended first-phase defaults:

| Setting | Default | Range / Clamp | Notes |
| --- | ---: | ---: | --- |
| `yield_ms` | `10000` ms | `10..120000` ms | Same shape as OpenClaw's default foreground yield window. |
| `timeout` | `60` s | `1..(tool_outer_timeout - 5)` s | Hard kill timeout owned by `command`. Must be lower than the framework/tool-manager outer timeout so the tool can return `timed_out` with captured output. |
| `input_wait_idle_ms` | `15000` ms | `1000..600000` ms | Input-wait hint threshold. |
| `process.poll timeout` | `0` ms | `0..30000` ms | Optional wait before draining pending output. |
| `max_output_chars` | `200000` | `1000..200000` | Aggregated output cap. |
| `pending_max_output_chars` | `30000` | `1000..200000` | Pending output cap drained by `poll`. |
| `finished_ttl_ms` | `1800000` ms | `60000..10800000` ms | Finished session cleanup window. |

The defaults should be configurable on the manager/tool config, not repeated in model-facing prompts. Tool descriptions should explain behavior, not ask the model to manually supply default values.

Framework timeout ordering is mandatory:

```text
yield_ms < command.timeout < command_tool_outer_timeout <= turn/tool runtime timeout
```

`command.timeout` is the semantic timeout that kills the running process and returns a structured `timed_out` result with captured terminal output. The framework/tool-manager outer timeout is only an envelope to prevent implementation hangs. It must be configured larger than `command.timeout` by a safety margin, recommended `command.timeout + 10s`. If the outer timeout fires first, the framework will lose the chance to return the terminal output and timeout details, which is a correctness bug.

### `process`

Purpose: manage command sessions that are running or recently finished.

Actions:

- `list`: show running and recent finished sessions.
- `poll`: optionally wait up to `timeout` milliseconds, drain pending output, report current status.
- `log`: read aggregated output with `offset` and `limit`.
- `write`: write raw data to stdin.
- `submit`: send carriage return/enter.
- `send_keys`: send encoded key tokens or hex bytes.
- `paste`: paste text, using bracketed paste where supported.
- `interrupt`: send Ctrl+C without destroying the session.
- `kill`: terminate the process/session.
- `clear`: remove a finished session from the registry.
- `remove`: remove a running or finished session; running sessions are killed first.

Parameters:

```python
{
    "action": "list | poll | log | write | submit | send_keys | paste | interrupt | kill | clear | remove",
    "session_id": "string, required except list",
    "data": "string, for write",
    "keys": "list[str], for send_keys",
    "hex": "list[str], for send_keys",
    "literal": "string, for send_keys",
    "text": "string, for paste",
    "bracketed": "bool, for paste",
    "eof": "bool, for write",
    "offset": "int, for log",
    "limit": "int, for log",
    "timeout": "int milliseconds, for poll; clamp to a configured max",
}
```

`poll` semantics:

- Returns only newly pending stdout/stderr since the previous drain.
- If the process has exited, moves it to finished state and returns final status.
- If still running and no new output is available, returns `(no new output)` plus a running status.
- Adds input-wait hints when `stdin_writable=true` and `idle_ms >= input_wait_idle_ms`.
- If a command timed out before or during poll-visible lifecycle handling, return `status=timed_out`, `timed_out=true`, and include the captured pending output plus a message explaining that the process was terminated after the configured timeout.

### `terminal`

Purpose: manage named terminal tabs/sessions, not execute commands.

Actions:

- `open`: create or start a named terminal.
- `close`: close a named terminal and terminate associated live process if needed.
- `list`: list known terminals.
- `select`: set default terminal.
- `history`: return recent command records for a terminal.
- `interrupt`: send Ctrl+C to the selected/current terminal.
- `current`: return the current terminal segment from the last command line/prompt to now.

`terminal current`:

- Returns the content visible or buffered from the last command line/prompt through the current cursor state.
- If no command has ever been entered, the empty prompt/current input line is still a valid current segment.
- Includes unsubmitted text on the command line when the backend can observe it.
- Does not depend on command history caches as the source of truth. The backend must read the current screen/PTY state.
- Exposes the same result shape for visible and hidden managers. Backend-specific limitations appear as diagnostics, not as different tool semantics.

## Abstraction Layers

### Platform and Shell Types

Keep typed enums and immutable descriptors:

```python
class Platform(StrEnum):
    WINDOWS = "windows"
    LINUX = "linux"
    DARWIN = "darwin"

class ShellFamily(StrEnum):
    BASH = "bash"
    CMD = "cmd"
    POWERSHELL = "powershell"
    SH = "sh"
    ZSH = "zsh"

class TerminalVisibility(StrEnum):
    VISIBLE = "visible"
    HIDDEN = "hidden"

@dataclass(frozen=True)
class ShellInfo:
    family: ShellFamily
    path: str
    platform: Platform
```

First implementation supports Windows managers and the shell families that can be reliably launched through the selected Windows backend. Unsupported combinations fail explicitly during manager construction or tool execution.

### TerminalManager Protocol

Tools depend on a protocol, not concrete managers:

```python
class TerminalManager(Protocol):
    platform: Platform
    shell_info: ShellInfo
    visibility: TerminalVisibility

    async def get_or_create(self, name: str | None, workdir: str | None = None) -> TerminalSession: ...
    async def get_default(self) -> TerminalSession: ...
    async def select_default(self, name: str) -> None: ...
    async def list_sessions(self) -> list[TerminalInfo]: ...
    async def close(self, name: str) -> bool: ...
```

Concrete first-phase managers:

- `WindowsVisibleTerminalManager`
- `WindowsHiddenTerminalManager`

The tool layer receives only `TerminalManager`. It must not branch on visible/hidden for behavior decisions.

### TerminalBackend Protocol

Backends own OS-level process and terminal I/O:

```python
class TerminalBackend(Protocol):
    platform: Platform
    visibility: TerminalVisibility

    async def start(self, shell: ShellInfo, cwd: str | None, env: dict[str, str] | None) -> None: ...
    async def write(self, data: str) -> None: ...
    async def read_pending(self, timeout: float, max_size: int) -> TerminalRead: ...
    async def current_segment(self) -> TerminalSegment: ...
    async def interrupt(self) -> None: ...
    async def terminate(self) -> None: ...
    async def kill(self) -> None: ...
    async def is_alive(self) -> bool: ...
    def stdin_writable(self) -> bool: ...
```

Concrete first-phase backends:

- `WindowsVisiblePtyBackend`: launches a user-visible console host and communicates through the existing socket/winpty style bridge.
- `WindowsHiddenPtyBackend`: launches the same shell through a hidden PTY host without a visible window.

Both backends should implement the same read/write/current/terminate contract.

## Session Model

### TerminalSession

Represents a named tab/session regardless of visible/hidden implementation.

Responsibilities:

- Own one backend.
- Track current command process metadata.
- Start command execution.
- Write input.
- Poll pending output.
- Report current terminal segment.
- Manage lifecycle and restart policy.

### ProcessSession

Represents one command run.

Fields:

```python
@dataclass
class ProcessSession:
    id: str
    terminal: str
    command: str
    pid: int | None
    cwd: str | None
    started_at: float
    ended_at: float | None
    status: ProcessStatus
    stdin_writable: bool
    last_output_at: float
    pending_stdout: list[str]
    pending_stderr: list[str]
    aggregated: str
    tail: str
    total_output_chars: int
    max_output_chars: int
    pending_max_output_chars: int
    truncated: bool
    exit_code: int | None
    exit_signal: str | int | None
```

`ProcessRegistry` owns running and finished `ProcessSession` records. It must provide:

- unique session id generation;
- add/get/delete session;
- append output with pending and aggregate caps;
- drain pending output;
- mark exited and move to finished;
- TTL cleanup for finished sessions.

## Output and State Rules

- Pending output is for `poll`; it is drained once.
- Aggregated output is for `log`; it is retained up to `max_output_chars`.
- Tail is a short diagnostic summary for `list` and running command responses.
- Model-facing output is sanitized for ANSI/OSC/DA1/control noise.
- Visible terminal output remains visually faithful in the user window.
- `waiting_for_input` is not a terminal status. It is a runtime hint attached to running sessions.
- A command can be `running` and `waiting_for_input=true` at the same time.
- Input handling must be byte-oriented. `process write`, `submit`, `send_keys`, and `paste` write to the active PTY/session without assuming the current program is the launch shell.
- Results must not be split by local shell prompts alone. Prompt detection can improve `terminal current`, but `command/process` status must continue to work when the session is inside SSH, nested shells, password prompts, pagers, REPLs, editors, or interactive installers.

## Remote, Nested Shell, and Interactive Input Semantics

Terminal sessions are stateful streams. A command can change the active environment without changing the manager abstraction:

- `ssh user@host` moves the visible/hidden tab into a remote OS.
- `sudo -i`, `su`, `cmd`, `powershell`, `bash`, `python`, `node`, and similar commands can enter nested shells or REPLs.
- `git diff`, `less`, package managers, installers, and CLIs may open pagers or prompt for confirmation.
- Password prompts often do not echo input; output may remain idle while stdin is writable.

The tool layer must therefore preserve these invariants:

1. `ShellInfo` describes the shell used to launch the session. It does not claim to describe the currently active remote shell or foreground program.
2. `process write` sends bytes exactly as requested.
3. `process submit` sends the backend's configured Enter sequence, not a shell command.
4. `process send_keys` is for control keys such as arrows, Escape, Ctrl+C, Ctrl+D, PageUp/PageDown, and function keys.
5. `process paste` is for larger text payloads; use bracketed paste when supported, but fall back to raw paste when the backend cannot confirm support.
6. `waiting_for_input=true` means "stdin is writable and no output has arrived for the idle threshold." It does not assert that the process definitely needs input.
7. Tool descriptions must teach the model to inspect the latest output before sending sensitive or destructive input. For yes/no prompts, the model should answer only when the prompt text is visible or the user explicitly instructed the answer.
8. Password/passphrase/token prompts should be surfaced as input-wait hints. The framework should not invent secrets. If the model does not have the credential, it should ask the user or let the user type directly in a visible tab.
9. Hidden and visible managers must return the same result shapes for SSH/nested shell scenarios. The only difference is that a visible tab also allows direct human intervention.

`terminal current` is especially important for these cases. It should show the latest active prompt, partial command line, pager screen, password prompt, REPL prompt, SSH banner, or confirmation question even when no command has completed.

## Approval and Safety

The redesign must keep the existing approval architecture invariant:

`ToolNode -> ApprovalTransaction -> TurnSnapshot -> ApprovalRenderer`

Do not move approval into terminal managers, backends, hooks, or interceptors.

Approval classifier should treat `command` as the replacement for `shell`. Path/argument matching should inspect `command`, `workdir`, and environment overrides. `process write/send_keys/paste/submit/interrupt/kill` should be separately classifiable because they can affect an already-approved process.

## Bot Project Integration

Pool configuration should choose the manager kind:

```yaml
terminal:
  manager: "windows_visible"  # or "windows_hidden"
  shell: "bash"               # first-phase supported values are implementation-defined
  storage_dir: "data/terminals/main"
  max_terminals: 5
  input_wait_idle_ms: 15000
  finished_ttl_ms: 1800000
```

Agent tool registration remains identical regardless of manager kind:

- register `CommandTool(manager, registry)`;
- register `ProcessTool(registry)`;
- register `TerminalTool(manager)`.

If the configured manager cannot initialize, startup should fail loudly or disable command tools with an explicit diagnostic. It must not silently create a subprocess fallback.

## Testing Strategy

Unit tests:

- tool schemas for `command`, `process`, and `terminal`;
- manager protocol behavior with fake visible/hidden managers;
- process registry pending/aggregated output caps and drain behavior;
- waiting-for-input hint after idle threshold;
- `poll` does not repeat drained output;
- `log` can page aggregated output;
- `kill/remove` state transitions;
- `terminal current` returns the last segment including empty prompt state;
- approval classifier uses `command` instead of legacy `shell`.

Windows integration tests:

- hidden manager can start a command, return completed output, and never use subprocess;
- hidden long-running command yields and can be polled/killed;
- hidden interactive command accepts `process write` and `submit`;
- visible manager opens a real visible tab and command output is visible to the backend;
- visible `terminal current` reports the last command/current input segment;
- visible and hidden managers produce the same tool result shapes for equivalent operations.

Bot project tests:

- main/coding pools register the same tools regardless of visible/hidden manager;
- pool-specific terminal storage is isolated;
- no `ShellTool`, `SubprocessExecutor`, or `TerminalSessionExecutor` is registered.

## Migration Plan

1. Introduce new terminal types, manager protocol, backend protocol, result dataclasses, and process registry.
2. Implement Windows hidden backend and hidden manager.
3. Implement Windows visible backend/manager using the existing visible host code where possible.
4. Add `CommandTool`, `ProcessTool`, and updated `TerminalTool`.
5. Wire bot_project pool and pipeline registration to the new tools.
6. Update approval config examples from `shell` to `command`.
7. Delete legacy `ShellTool`, `SubprocessExecutor`, and `TerminalSessionExecutor` registration paths.
8. Update docs and tests.

## Open Extension Points

- macOS and Linux managers can later implement the same `TerminalManager` protocol.
- Additional shell families can be added by extending `ShellInfo`, shell launch policies, command terminators, and key encoding.
- Remote execution and sandbox execution should be separate manager/backend implementations, not special cases inside tools.
