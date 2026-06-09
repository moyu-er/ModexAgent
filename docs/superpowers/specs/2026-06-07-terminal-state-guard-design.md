# Terminal State Detection & Input Guard Design

**Date**: 2026-06-07
**Status**: Draft

## Problem

The terminal tool system (CommandTool / ProcessTool / TerminalTool) has critical gaps:

1. **No state discrimination**: Long-running (`git clone`), repaint-progress (`pip install`), blocked/hung, and normal executing commands are all treated the same. The agent cannot distinguish them.

2. **No input guard**: CommandTool and ProcessTool.write accept commands regardless of terminal state. If a terminal is busy, the input corrupts the running command's stdin and produces incomprehensible errors.

3. **Rough STUCK detection**: Uses a fixed 15s `_last_byte_at` threshold with no output-chunk-based reset. `sleep 10` is falsely marked STUCK.

4. **WAITING_INPUT must not be blocked**: The guard must accurately pass through genuine input-wait scenarios (password prompts, y/n confirmations).

5. **Test quality**: 28 existing test files are shallow — they verify mock calls, not actual behavior. No cross-tool integration tests, no abnormal scenario coverage.

6. **Visible terminal interference**: In visible terminal windows, users share the same PTY as the agent. User keystrokes can corrupt agent commands, trigger unexpected state transitions, or kill the shell.

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Guard strategy | Hard reject + diagnostic snapshot | Agent must resolve blocking state before retrying |
| State model | Add LONG_RUNNING; keep STUCK with no-output-timeout | PTY persistent sessions need STUCK; sleep handled by is_alive() check |
| STUCK detection | no-output-timeout (per-chunk reset) | Borrowed from OpenClaw `touchOutput`; each output chunk resets the timer |
| Guard scope | CommandTool.execute + ProcessTool._do_write only | Interrupt/kill/send_keys/paste/terminal management are not guarded |
| Test strategy | Delete all 28 test files, rewrite 5-6 | Quality over quantity |
| Visible terminal interference | Detect + warn (not prevent) | Shared PTY cannot be locked; detect unexpected state transitions and warn agent |

## Section 1: State Model

### TerminalCommandStatus (enhanced)

```
UNKNOWN        — No data ever received (safety net)
IDLE           — Prompt stable, no command running
EXECUTING      — Active output (including repaint progress), idle < no_output_timeout
LONG_RUNNING   — Command running > threshold, still alive, idle < no_output_timeout
STUCK          — No output for no_output_timeout duration
WAITING_INPUT  — Content-based input marker detected
PAGINATED      — Inside pager (less/more)
COMPLETED      — Process exited
TIMED_OUT      — Command killed by overall timeout
```

### `command_status()` detection priority

```
1. is_alive() == False                        → COMPLETED
2. !_ever_received_bytes                      → UNKNOWN
3. Content input marker (is_waiting_for_input)→ WAITING_INPUT
4. segment.is_empty_prompt                    → IDLE
5. Pager detection                            → PAGINATED
6. idle >= no_output_timeout_ms               → STUCK
7. elapsed >= long_running_threshold AND alive → LONG_RUNNING
8. Default                                    → EXECUTING
```

**Key change for step 6**: Replace `_last_byte_at` 15s hard threshold with `no_output_timeout_ms` (default 30s). The timer resets on every output chunk via `touch_output()` on the session. `touch_output()` is called in `session.poll_once()` when non-empty output is received.

**Sleep handling**: `sleep 10` has no output for 10s but `is_alive()` is True. Step 6 uses idle time only (not is_alive). If idle < 30s → EXECUTING. If idle >= 30s → STUCK. This is acceptable because 30s no-output with a live process is genuinely ambiguous. The threshold is configurable.

**LONG_RUNNING detection**: `command_status()` needs to know when the current command started. This is passed via an optional `command_started_at: float | None = None` parameter, sourced from `ProcessSession.started_at` in the registry. When `command_started_at` is None (no registered process), LONG_RUNNING is never returned.

## Section 2: Guard Mechanism

### New file: `framework/tools/terminal/guard.py`

```python
@dataclass
class TerminalSnapshot:
    status: TerminalCommandStatus
    cursor_line: str
    last_output: str       # truncated to 2000 chars
    idle_ms: int
    elapsed_ms: int | None
    suggestion: str

@dataclass
class TerminalGuardResult:
    status: TerminalCommandStatus
    message: str
    snapshot: TerminalSnapshot

async def check_terminal_writable(
    session: TerminalSession,
    registry: ProcessRegistry | None = None,
) -> TerminalGuardResult | None:
    """Check if terminal is ready for new input.

    Returns None if writable (proceed), or GuardResult with diagnostic snapshot.
    """
```

### Guard logic

```
status = await session.command_status()

Allowed (return None):
  IDLE, UNKNOWN, WAITING_INPUT, COMPLETED, TIMED_OUT

Rejected (return GuardResult):
  EXECUTING, LONG_RUNNING, STUCK, PAGINATED
```

WAITING_INPUT is explicitly allowed — the terminal is waiting for input, so writing is correct.

### Guard placement

| Tool | Entry point | Guarded? |
|---|---|---|
| CommandTool | `execute()` | Yes — at the start, before `submit_command` |
| ProcessTool | `_do_write()` | Yes — at the start |
| ProcessTool | `_do_submit()`, `_do_send_keys()`, `_do_paste()` | No — interaction with running process |
| ProcessTool | `_do_interrupt()`, `_do_kill()` | No — emergency control, always allowed |
| TerminalTool | All actions | No — management operations |

### Rejection XML format (CommandTool)

```xml
<command_result>
  <status>rejected</status>
  <message>Terminal is not ready for new commands: command is still executing.</message>
  <terminal>default</terminal>
  <diagnostic>
    <status>executing</status>
    <idle_ms>3200</idle_ms>
    <cursor>Downloading packages... 45%</cursor>
    <last_output>...(up to 2000 chars)...</last_output>
    <suggestion>Use 'terminal current' to monitor progress,
                or 'process interrupt' to stop the running command.</suggestion>
  </diagnostic>
</command_result>
```

### Rejection format (ProcessTool write)

Same XML structure wrapped in `<process_result>`:
```xml
<process_result>
  <action>write</action>
  <status>rejected</status>
  <message>Cannot write: terminal command is still executing.</message>
  <diagnostic>...</diagnostic>
</process_result>
```

### Suggestion per status

| Status | Suggestion |
|---|---|
| EXECUTING | "Use 'terminal current' to monitor, or 'process interrupt' to stop." |
| LONG_RUNNING | "Command has been running for Ns. Use 'terminal current' to check, or 'process interrupt' to stop." |
| STUCK | "No output for Ns. Use 'process interrupt' to send Ctrl+C, or 'terminal current' to check screen." |
| PAGINATED | "Terminal is in a pager. Use 'process send_keys' with 'q' to quit, or Space to scroll." |

## Section 3: Two-Tier Timeout & PollOutcome

### Two-layer timeout

| Layer | Config key | Default | Meaning |
|---|---|---|---|
| no-output timeout | `no_output_timeout_ms` | 30,000 (30s) | Time since last output chunk with no new bytes |
| overall timeout | `default_command_timeout_seconds` | 60s | Total wall-clock time for a command |

**Core mechanism**: Every output chunk (stdout/stderr byte) calls `touch_output()` on the session, which resets the no-output timer. This is borrowed from OpenClaw's `touchOutput()` pattern.

- `git clone` with continuous output → no-output resets continuously → no false STUCK
- `pip install` with `\r` repaint progress → bytes flowing → no-output resets → no false STUCK
- `sleep 10` → 10s no output → idle < 30s threshold → stays EXECUTING (not STUCK)
- Network stall → long no-output → STUCK correctly detected

### TerminalRuntimeConfig additions

```python
no_output_timeout_ms: int = 30_000          # no-output STUCK threshold
long_running_threshold_ms: int = 300_000    # LONG_RUNNING detection threshold
```

### PollOutcome additions

```
PROCESS_EXIT      — Process exited (authoritative completion)
PROMPT_DETECTED   — Prompt stable detected (PTY auxiliary completion)
INPUT_WAIT        — Content-based input marker
STUCK             — no-output timeout + is_alive() True
LONG_RUNNING      — elapsed > long_running_threshold, still active
YIELDED           — yield window expired (normal handoff)
TIMED_OUT         — overall timeout (hard limit, kills process)
```

### `poll_until_settled()` changes

In the main loop, replace current stuck detection with:

```python
# 4. No-output timeout (replaces old stuck detection)
raw_idle_ms = int((time.monotonic() - session.last_byte_at) * 1000)
if raw_idle_ms >= config.no_output_timeout_ms:
    if not await session.is_alive():
        return PollResult(PollOutcome.PROCESS_EXIT, output_parts, elapsed_ms)
    if not is_waiting_for_input("".join(output_parts)):
        return PollResult(PollOutcome.STUCK, output_parts, elapsed_ms)

# 4.5 Long-running detection (before yield)
if elapsed_ms >= config.long_running_threshold_ms:
    if output_received and await session.is_alive():
        return PollResult(PollOutcome.LONG_RUNNING, output_parts, elapsed_ms)

# 5. Yield window (unchanged)
if elapsed_ms >= yield_ms:
    return PollResult(PollOutcome.YIELDED, output_parts, elapsed_ms)
```

## Section 4: TerminalTool.current Improvements

1. **Use ProcessRegistry aggregated output** when a running process exists, instead of relying solely on `extract_last_command_output()` heuristic
2. **Better cursor content** for STUCK/LONG_RUNNING: fallback to output buffer's last few lines when `resolve_cursor_line` returns empty
3. **Add `elapsed_ms`** field to current output showing how long the current command has been running

## Section 5: Test Strategy

### Delete all existing test files under `tests/framework/tools/terminal/`

### New test files

#### `test_guard.py` — Guard mechanism

| Test case | Validates |
|---|---|
| EXECUTING → reject command | Guard returns rejection XML with diagnostic |
| IDLE → allow command | Guard returns None, command executes |
| WAITING_INPUT → allow write | Guard passes through input-wait state |
| STUCK → reject command | Guard returns rejection with suggestion |
| LONG_RUNNING → reject command | Guard returns rejection with elapsed time |
| Interrupt bypasses guard | `_do_interrupt` works regardless of state |
| Diagnostic snapshot content | Verify cursor_line, idle_ms, suggestion are populated |

#### `test_status_detection.py` — State detection

| Test case | Validates |
|---|---|
| is_alive=False → COMPLETED | Dead process correctly detected |
| No bytes received → UNKNOWN | Safety net for uninitialized sessions |
| Input marker content → WAITING_INPUT | Password/confirm prompts detected |
| Stable prompt → IDLE | Shell prompt at rest detected |
| idle >= no_output_timeout → STUCK | No-output timeout fires correctly |
| Output resets no-output timer | Continuous output prevents STUCK |
| elapsed > threshold → LONG_RUNNING | Long-running threshold triggers |
| Active output → EXECUTING | Default active state |

#### `test_tool_integration.py` — Cross-tool workflows

| Test case | Validates |
|---|---|
| Command → Process.write → Terminal.current | Full happy path |
| Command executing → new command rejected | Guard blocks concurrent commands |
| Command executing → interrupt → command allowed | Recovery flow |
| Command stuck → terminal current shows diagnostic | Diagnostic output accuracy |
| Multiple tabs: busy tab blocks, idle tab allows | Guard is per-session |

#### `test_poll_loop.py` — Poll logic

| Test case | Validates |
|---|---|
| Process exit detected | PollOutcome.PROCESS_EXIT |
| Prompt stabilization detected | PollOutcome.PROMPT_DETECTED |
| No-output timeout → STUCK | PollOutcome.STUCK with correct threshold |
| Long-running detection | PollOutcome.LONG_RUNNING |
| Yield window expiry | PollOutcome.YIELDED |
| Overall timeout kills process | PollOutcome.TIMED_OUT |

#### `test_prompt_detection.py` — Prompt & input detection

| Test case | Validates |
|---|---|
| Password prompt detected | `is_waiting_for_input` accuracy |
| Repaint progress not input wait | `\r` progress bars don't false-trigger |
| ANSI pollution filtered | ConPTY sequences don't break detection |
| Multi-shell prompt detection | `$`, `#`, `>`, `%` recognized |
| Ambiguous markers need punctuation | "password" in output requires `:` ending |

#### `test_types.py` — Type enums & helpers

| Test case | Validates |
|---|---|
| TerminalCommandStatus values | All enum values present |
| ShellFamily.command_ending | `\n` vs `\r\n` per family |
| Platform detection | detect_platform_shell returns valid ShellInfo |

### Test quality principles

- Every test validates **concrete behavior** (XML content, status values, guard pass/reject), not mock call counts
- Use `FakeBackend` to simulate output patterns (continuous, repaint, silent, input-prompt)
- Cover cross-tool interaction (CommandTool creates process → ProcessTool interacts → TerminalTool inspects)
- Cover abnormal scenarios (stuck, long-running, concurrent command rejection)

## Section 6: Visible Terminal Anti-Interference

### Problem

Visible terminals (`VisibleWindowsPtyBackend`) share the PTY between agent and user. User keystrokes can:
- Corrupt agent commands (mixed input)
- Trigger unexpected state transitions (EXECUTING → IDLE from user pressing Enter)
- Kill the shell (user types `exit`)
- Terminate the backend (user closes the window)

### Strategy: Detect + Warn

Physical prevention is impossible — the user has direct access. Instead, detect unexpected state transitions and warn the agent.

### Expected state tracking

Add to `TerminalSession`:

```python
_expected_state: TerminalCommandStatus | None = None

def set_expected_state(self, status: TerminalCommandStatus) -> None:
    """Set the expected terminal state after an agent operation."""
    self._expected_state = status

def detect_interference(self, actual: TerminalCommandStatus) -> bool:
    """Detect if actual state diverges from expected (possible user interference)."""
    if self._expected_state is None:
        return False
    unexpected = {
        (TerminalCommandStatus.EXECUTING, TerminalCommandStatus.IDLE),
        (TerminalCommandStatus.LONG_RUNNING, TerminalCommandStatus.IDLE),
    }
    return (self._expected_state, actual) in unexpected
```

### State lifecycle

```
CommandTool.execute() starts:
  → session.set_expected_state(EXECUTING)

CommandTool.execute() returns:
  → if PROMPT_DETECTED: session.set_expected_state(IDLE)
  → if YIELDED/LONG_RUNNING: session.set_expected_state(EXECUTING/LONG_RUNNING)
  → if STUCK/TIMED_OUT: session.set_expected_state(None)  # needs resolution

Guard check or terminal current:
  actual = await session.command_status()
  if session.detect_interference(actual):
    → append <interference_warning> to result
```

### Warning XML format

```xml
<interference_warning>
  Terminal state changed unexpectedly (was: executing, now: idle).
  This may be caused by user input in the visible terminal window.
  Current screen content is shown above — verify before proceeding.
</interference_warning>
```

### Applicable locations

- `check_terminal_writable()` — guard includes interference check in diagnostic
- `TerminalTool` current action — appended to result when detected

### NOT applicable to hidden terminals

Hidden terminals (`WindowsHiddenPtyBackend`, `TmuxPtyBackend`) have no user-facing window, so interference is impossible. The `_expected_state` tracking is only active when `session.visible == True`.

## Scenario Coverage Matrix

| Scenario | Detection | Guard | Recovery |
|---|---|---|---|
| Blocking (network hang) | no-output-timeout → STUCK | Reject + diagnostic | process interrupt → IDLE |
| Repaint progress (`pip install`) | Bytes flow → EXECUTING | Reject (command busy) | Agent monitors via terminal current |
| Long-running (`git clone`) | Continuous output → EXECUTING → LONG_RUNNING | Reject (command busy) | Agent monitors, waits for completion |
| Interactive input (`password:`) | Content marker → WAITING_INPUT | **Pass through** | Agent asks user → process write |
| `sleep` commands | idle < no_output_timeout → EXECUTING | Reject (command busy) | Waits for completion naturally |
| User types in visible terminal | Unexpected state transition → interference_warning | Warning in diagnostic | Agent re-checks state |
| User closes visible window | backend dies → COMPLETED | No guard needed (terminal reset) | Auto-restart on next command |

## Files Changed

### New files
- `framework/tools/terminal/guard.py` — `check_terminal_writable()`, `TerminalGuardResult`, `TerminalSnapshot`
- `tests/framework/tools/terminal/test_guard.py`
- `tests/framework/tools/terminal/test_status_detection.py`
- `tests/framework/tools/terminal/test_tool_integration.py`
- `tests/framework/tools/terminal/test_poll_loop.py`
- `tests/framework/tools/terminal/test_prompt_detection.py`
- `tests/framework/tools/terminal/test_types.py`

### Modified files
- `framework/tools/terminal/types.py` — Add `LONG_RUNNING` to `TerminalCommandStatus`, add `CommandResultStatus.REJECTED`
- `framework/tools/terminal/session.py` — Enhance `command_status(command_started_at=)` with no-output-timeout + LONG_RUNNING detection; add `touch_output()` method; add `set_expected_state()` / `detect_interference()` for visible terminal anti-interference
- `framework/tools/terminal/poll_loop.py` — Add `LONG_RUNNING` to `PollOutcome`; replace stuck detection with no-output-timeout; add long-running detection
- `framework/tools/terminal/config.py` — Add `no_output_timeout_ms`, `long_running_threshold_ms`
- `framework/tools/terminal/command_tool.py` — Add guard call at start of `execute()`; handle `LONG_RUNNING` PollOutcome; handle rejected guard result
- `framework/tools/terminal/process_tool.py` — Add guard call at start of `_do_write()`; handle rejected guard result
- `framework/tools/terminal/tool.py` — Improve `current` action output accuracy

### Deleted files
- All 28 existing test files under `tests/framework/tools/terminal/`
