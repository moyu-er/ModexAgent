# Terminal System Improvements Design

**Date**: 2026-05-30
**Status**: Approved
**Scope**: `framework/tools/terminal/`

## Overview

Fix 5 concrete issues in the terminal system's command execution, pager handling, state awareness, command submission reliability, and memory management. One additional item (human-agent mixed input detection) is deferred as a TODO comment.

## Scope Summary

| # | Improvement | Status | Priority |
|---|-------------|--------|----------|
| 1 | Tiered idle timeout | Implement | P1 |
| 2 | Pager auto-scroll | Implement | P1 |
| 3 | `terminal current` structured status | Implement | P1 |
| 4 | Human-agent mixed input detection | **TODO comment only** | Deferred |
| 5 | Unified command submission path | Implement | P0 |
| 6 | Memory leak fix (sliding buffer) | Implement | P0 |

## Architecture Changes

### Base Class Promotion

Common logic moves from individual backends up to `TerminalBackend` (base.py):

- `_output_buffer: SlidingOutputBuffer | None` — shared sliding buffer attribute
- `mark_command_boundary()` — called by session before each command
- `_append_to_buffer(text)` — called by backends during read
- `extract_current_segment_from_buffer()` — moved from `visible_windows.py` to `base.py`

Each backend only initializes its own `SlidingOutputBuffer` in `__init__`. Tmux backend keeps `_output_buffer = None` (uses `_last_capture` which is naturally bounded).

**`super().__init__()` requirement**: The base class gains an `__init__` that sets `_output_buffer = None`. All existing backends must add `super().__init__()` as the first line of their `__init__` methods. Backends that use the sliding buffer then override it: `self._output_buffer = SlidingOutputBuffer()`.

### New Type: `SlidingOutputBuffer`

Dual-constraint sliding window replacing the unbounded `_output_buffer: str`:

- **Character constraint**: max 200,000 chars
- **Command constraint**: max 100 command blocks (via `deque(maxlen=100)`)
- Both constraints enforced simultaneously; whichever is stricter wins

### New Enum: `TerminalStatus`

```python
class TerminalStatus(StrEnum):
    OK = "ok"
    TIMEOUT = "timeout"
    BUSY = "busy"
    WAITING_INPUT = "waiting_input"
    ENDED = "ended"
    PAGINATED = "paginated"  # new
```

Used in `<shell_result>` XML responses (session.py path).

### Unified XML Return Format

All agent-facing tool returns use structured XML with explicit status fields, enabling the model to programmatically distinguish completion states.

**Layer separation**:

| Layer | XML Root Tag | Scope |
|-------|-------------|-------|
| `session.execute()` | `<shell_result>` | Internal session-level exceptions (unchanged) |
| `CommandTool` | `<command_result>` | Agent-facing command execution |
| `ProcessTool` | `<process_result>` | Agent-facing process interaction |
| `TerminalTool` | `<terminal_result>` | Agent-facing terminal management |

**`CommandResultStatus` enum** (new, in `types.py`):

```python
class CommandResultStatus(StrEnum):
    COMPLETED = "completed"      # Command finished normally
    RUNNING = "running"          # Command still executing (yield_ms reached)
    TIMED_OUT = "timed_out"      # Hard timeout, process terminated
    PAGINATED = "paginated"      # Pager auto-scrolled
    INPUT_WAIT = "input_wait"    # Likely waiting for user input
```

**`<command_result>` field spec**:

| Field | Type | Required | Condition | Description |
|-------|------|----------|-----------|-------------|
| `<output>` | string | ✅ | Always | Command output (xml_escape) |
| `<status>` | enum | ✅ | Always | `CommandResultStatus` value |
| `<elapsed_ms>` | int | ✅ | Always | Time since command was submitted |
| `<idle_ms>` | int | ❌ | running, input_wait | Time since last output chunk |
| `<pages_scrolled>` | int | ❌ | paginated | Number of auto-scroll pages |
| `<truncated>` | bool | ❌ | paginated | Whether output was cut off |
| `<message>` | string | ❌ | running, timed_out, input_wait, paginated | Human-readable explanation |

**`<command_result>` examples**:

Completed:
```xml
<command_result>
<output>$ ls -la
total 42
drwxr-xr-x  5 user group 4096 May 30 10:00 .
...</output>
<status>completed</status>
<elapsed_ms>234</elapsed_ms>
</command_result>
```

Running:
```xml
<command_result>
<output>Installing dependencies...
added 142 packages in 8s</output>
<status>running</status>
<elapsed_ms>10500</elapsed_ms>
<idle_ms>800</idle_ms>
<message>Command still running. Use process log for latest output, 
process write/send_keys for input.</message>
</command_result>
```

Timed out:
```xml
<command_result>
<output>Building project...
[1/5] Compiling module A...</output>
<status>timed_out</status>
<elapsed_ms>60000</elapsed_ms>
<message>Command timed out after 60s and was terminated. 
Partial output captured above.</message>
</command_result>
```

Paginated:
```xml
<command_result>
<output>diff --git a/src/main.py b/src/main.py
...full diff content across 8 pages...</output>
<status>paginated</status>
<elapsed_ms>25000</elapsed_ms>
<pages_scrolled>8</pages_scrolled>
<truncated>false</truncated>
<message>Output was displayed through a pager and automatically scrolled. 
If content was cut off, use process send_keys keys=[" "] to continue.</message>
</command_result>
```

Input wait:
```xml
<command_result>
<output>Enter password for user@host:</output>
<status>input_wait</status>
<elapsed_ms>5200</elapsed_ms>
<idle_ms>3100</idle_ms>
<message>No new output for 3s; session may be waiting for input. 
Use process write data="VALUE" submit=true to provide input.</message>
</command_result>
```

**`<process_result>` field spec**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `<action>` | string | ✅ | The action performed (log, write, send_keys, list, etc.) |
| `<output>` | string | ✅ | Result text (xml_escape) |
| `<session_id>` | string | ❌ | Process session ID when applicable |
| `<status>` | string | ❌ | Process status (running, completed, etc.) |
| `<idle_ms>` | int | ❌ | Idle time for running processes |
| `<bytes_written>` | int | ❌ | For write action |
| `<sessions>` | XML | ❌ | For list action — structured session entries |

**`<terminal_result>` for `terminal current`**:

```xml
<terminal_result>
<action>current</action>
<status>idle</status>
<cursor>$</cursor>
<output>...last 30 lines of terminal content...</output>
</terminal_result>
```

Status values: `idle`, `active`, `busy`, `waiting_input`, `pager`.

**Governance compatibility**: `<output>` is the truncatable content field (same as existing `<shell_result>`). Metadata fields (`<status>`, `<elapsed_ms>`, etc.) are preserved during truncation.

---

## Detailed Design

### 1. Tiered Idle Timeout

**Problem**: `input_wait_early_min_elapsed_ms` (2s) is too aggressive. Commands like `npm install` have multi-second quiet phases that get misidentified as "waiting for input".

**Files**: `config.py`, `process_registry.py`

**config.py changes**:

Replace `input_wait_early_min_elapsed_ms` with two tiered thresholds:

```python
@dataclass(frozen=True)
class TerminalRuntimeConfig:
    # Phase 1: command just sent, never produced output
    initial_idle_threshold_ms: int = 5_000
    # Phase 2: command produced output before, currently stalled
    active_idle_threshold_ms: int = 15_000
    # Keep existing input_wait_idle_ms (10s) for formal_waiting
```

**process_registry.py changes** in `running_runtime()`:

```python
if not session._output_timestamps:
    # Never produced output → use initial threshold (5s)
    threshold = self._config.initial_idle_threshold_ms
else:
    # Produced output before but currently stalled → use active threshold (15s)
    threshold = self._config.active_idle_threshold_ms

early_waiting = (
    session.stdin_writable
    and not velocity.is_active
    and idle_ms >= threshold
)
```

---

### 2. Pager Auto-Scroll

**Problem**: `git diff` / `git log` trigger `less` pager. Current implementation either times out or blocks. Agent cannot navigate pager output.

**Files**: `command_tool.py`, `session.py`, `prompt.py`, `backends/base.py`

**Design**: Detect pager entry via `:` marker, then auto-scroll by sending Space repeatedly until no new output arrives (behavior-driven termination, not marker enumeration).

#### 2a. Pager Entry Detection (`prompt.py`)

```python
_PAGER_ENTRY_MARKER = ":"

def detect_pager_entry(cursor_line: str) -> bool:
    """Detect if cursor line is a pager entry prompt (less colon).
    
    Only matches bare ":" on its own line. Excludes "config:", "error:", etc.
    """
    return cursor_line.strip() == _PAGER_ENTRY_MARKER
```

**cursor_line fallback**: The tmux backend's `current_segment()` does not populate `cursor_line` (defaults to `""`). When `cursor_line` is empty, fall back to the last non-empty line of `segment.text`:

```python
def _resolve_cursor_line(segment: TerminalSegment) -> str:
    """Get cursor line, falling back to last text line when backend doesn't provide it."""
    if segment.cursor_line:
        return segment.cursor_line
    lines = segment.text.splitlines()
    for line in reversed(lines):
        if line.strip():
            return line
    return ""
```

All pager detection call sites use `_resolve_cursor_line(segment)` instead of `segment.cursor_line` directly.

#### 2b. CommandTool Read Loop Addition (`command_tool.py`)

Insert pager detection between prompt detection and waiting_for_input check:

```python
# 4. Pager detection (new — before waiting_for_input)
if output_received and not read.stdout:
    idle_elapsed = time.monotonic() - last_output_time
    if idle_elapsed >= 2.0:  # 2s no new output
        segment = await session.current_segment()
        if (not segment.is_empty_prompt
                and detect_pager_entry(_resolve_cursor_line(segment))):
            output_parts, pages = await self._auto_scroll_pager(
                session, output_parts, proc.id
            )
            self._registry.mark_exited(proc.id, ...)
            return self._format_paginated(output_parts, pages)
```

#### 2c. Auto-Scroll Logic (`command_tool.py`)

```python
_PAGER_AUTO_SCROLL_MAX_PAGES = 10
_PAGER_AUTO_SCROLL_MAX_CHARS = 100_000

async def _auto_scroll_pager(self, session, initial_output, proc_id):
    """Auto-scroll pager until no new content or limit reached.
    
    Termination conditions (behavior-driven, no marker enumeration):
    1. Space sent, 2s no new output → reached end
    2. Page count reaches max (10)
    3. Total output chars reaches max (100K)
    4. Shell prompt appears → pager exited
    """
    output_parts = list(initial_output)
    total_chars = sum(len(p) for p in output_parts)
    pages_scrolled = 0
    
    for _ in range(_PAGER_AUTO_SCROLL_MAX_PAGES):
        await session.write(" ")  # Space = next page
        
        # Wait for new output (2s timeout = behavior-based end detection)
        new_output = False
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            read = await session.poll_once(timeout=0.3)
            if read.stdout:
                self._registry.append_output(proc_id, "stdout", read.stdout)
                output_parts.append(read.stdout)
                total_chars += len(read.stdout)
                new_output = True
                break
            if not await session.is_alive():
                break
        
        if not new_output:
            break  # Behavior: no new output → reached end
        
        pages_scrolled += 1
        if total_chars >= _PAGER_AUTO_SCROLL_MAX_CHARS:
            break
        
        segment = await session.current_segment()
        if segment.is_empty_prompt:
            return output_parts, pages_scrolled
    
    # Scrolling done, exit pager
    await session.write("q")
    await asyncio.sleep(0.5)
    while True:
        read = await session.poll_once(timeout=0.3)
        if not read.stdout:
            break
        output_parts.append(read.stdout)
    
    return output_parts, pages_scrolled
```

#### 2d. Return Format

CommandTool (natural language path):
```
...all collected output...

[paginated: auto-scrolled N pages]
If content was cut off, use process send_keys keys=[" "] to continue 
scrolling, or process send_keys keys=["q"] to exit the pager.
```

Session.py XML path (if applicable):
```xml
<shell_result>
<output>...xml_escaped output...</output>
<status>paginated</status>
<pages_scrolled>N</pages_scrolled>
<truncated>true|false</truncated>
<message>Output was displayed through a pager and automatically scrolled.</message>
</shell_result>
```

#### 2e. Remove PAGER Suppression

Remove `PAGER=cat`, `GIT_PAGER=cat`, `LESS=FRX` from `session._startup_env()`. Let pager work normally; the tool handles it.

---

### 3. `terminal current` Structured Status

**Problem**: `terminal current` returns raw text only. Agent cannot distinguish idle / busy / waiting_input / pager states.

**File**: `tool.py`

**Changes** to `TerminalAction.CURRENT` handler:

```python
if action_enum == TerminalAction.CURRENT:
    session = ...  # get session
    segment = await session.current_segment()
    cleaned = sanitize_terminal_output(segment.text).rstrip()
    
    # Infer status
    if segment.is_empty_prompt:
        status = "idle"
    elif session._busy_after_timeout:
        status = "busy"
    elif session._last_status == "waiting_input":
        status = "waiting_input"
    elif detect_pager_entry(_resolve_cursor_line(segment)):
        status = "pager"
    else:
        status = "active"
    
    lines = [f"[status: {status}]"]
    if segment.cursor_line:
        lines.append(f"[cursor: {segment.cursor_line.strip()}]")
    if cleaned:
        lines.extend(cleaned.splitlines()[-30:])
    else:
        lines.append("(terminal is idle — no output yet)")
    
    return "\n".join(lines)
```

---

### 4. Human-Agent Mixed Input Detection — DEFERRED

**Status**: TODO comment only. Not implemented in this change.

Add TODO comment in `visible_windows_host.py`:

```python
# TODO(terminal-human-input): Detect human keyboard input in visible terminal
# and notify the parent process via out-of-band socket marker (\x00HUMAN\x00).
# Parent filters the marker and sets a flag on the backend.
# Session layer checks the flag and appends a note to command results.
# Only affects VisibleWindowsPtyBackend (not hidden or tmux).
# Requires: socket write lock in host process to prevent marker interleaving.
```

---

### 5. Unified Command Submission Path

**Problem**: `CommandTool` calls `session.write(command + "\r")` directly, bypassing session's pre-execute cleanup (clear input line, correct line ending). This causes commands to be appended to stale input or not submitted at all.

**Files**: `session.py`, `command_tool.py`

#### 5a. New Method: `session.submit_command()`

```python
async def submit_command(self, command: str) -> None:
    """Submit a command to the PTY with proper pre-cleanup and line ending."""
    if self.shell_info.family.uses_readline():
        await self._discard_pending_output()
        await self._backend.clear_input_line()
        await asyncio.sleep(0.05)
        await self._discard_pending_output()
    
    ending = self.shell_info.family.command_ending()
    await self._backend.write(command + ending)
```

#### 5b. CommandTool Uses `submit_command()`

```python
async def execute(self, command, **_kwargs):
    session = await self._manager.get_default()
    await session.ensure_started()
    proc = self._registry.create(...)
    
    # Replace: await session.write(command + "\r")
    await session.submit_command(command)
    
    # ... rest of read loop unchanged ...
```

---

### 6. Memory Leak Fix — Sliding Buffer

**Problem**: `_output_buffer` in Windows backends grows without bound. After 100+ commands, `current_segment()` becomes slow due to string operations on multi-MB buffers.

**Files**: `results.py` (or new file), `backends/base.py`, `backends/visible_windows.py`, `backends/windows_hidden.py`, `session.py`, `manager.py`, `config.py`

#### 6a. `SlidingOutputBuffer` Class

```python
from collections import deque

class SlidingOutputBuffer:
    """Dual-constraint sliding window for terminal output.
    
    - Character constraint: total chars <= max_chars (200K)
    - Command constraint: keep last max_commands (100) command blocks
    - Both enforced simultaneously; stricter wins
    """
    
    def __init__(self, max_chars: int = 200_000, max_commands: int = 100):
        self._command_chunks: deque[str] = deque(maxlen=max_commands)
        self._current_parts: list[str] = []
        self._total_chars = 0
        self._max_chars = max_chars
    
    def append(self, text: str) -> None:
        self._current_parts.append(text)
        self._total_chars += len(text)
        self._trim_chars()
    
    def mark_command_boundary(self) -> None:
        if self._current_parts:
            chunk = "".join(self._current_parts)
            self._command_chunks.append(chunk)
            self._current_parts = []
            self._recalc_total_chars()
    
    @property
    def text(self) -> str:
        parts = list(self._command_chunks)
        if self._current_parts:
            parts.append("".join(self._current_parts))
        return "".join(parts)
    
    @property
    def total_chars(self) -> int:
        return self._total_chars
    
    def clear(self) -> None:
        self._command_chunks.clear()
        self._current_parts = []
        self._total_chars = 0
    
    def _trim_chars(self) -> None:
        while self._total_chars > self._max_chars and self._command_chunks:
            removed = self._command_chunks.popleft()
            self._total_chars -= len(removed)
    
    def _recalc_total_chars(self) -> None:
        self._total_chars = sum(len(c) for c in self._command_chunks)
        self._total_chars += sum(len(p) for p in self._current_parts)
```

#### 6b. Base Class Integration (`backends/base.py`)

```python
class TerminalBackend(ABC):
    def __init__(self) -> None:
        self._output_buffer: SlidingOutputBuffer | None = None
    
    def mark_command_boundary(self) -> None:
        if self._output_buffer is not None:
            self._output_buffer.mark_command_boundary()
    
    def _append_to_buffer(self, text: str) -> None:
        if self._output_buffer is not None:
            self._output_buffer.append(text)
```

Also move `extract_current_segment_from_buffer()` from `visible_windows.py` to `base.py`.

#### 6c. Backend Initialization

```python
# visible_windows.py
class VisibleWindowsPtyBackend(TerminalBackend):
    def __init__(self):
        super().__init__()
        self._output_buffer = SlidingOutputBuffer()
    
    async def read_pending(self, timeout, max_size):
        raw = await self.read(timeout=timeout, max_size=max_size)
        if raw:
            self._append_to_buffer(raw)
        return TerminalRead(stdout=raw, raw=raw)
    
    async def current_segment(self) -> TerminalSegment:
        return extract_current_segment_from_buffer(self._output_buffer.text)

# windows_hidden.py — same pattern
```

#### 6d. Session Calls `mark_command_boundary()`

```python
# session.py → execute()
async def execute(self, command, timeout):
    self._backend.mark_command_boundary()
    # ... rest of execute unchanged ...
```

#### 6e. Global Memory Pressure Detection (`manager.py`)

```python
# config.py
@dataclass(frozen=True)
class TerminalRuntimeConfig:
    max_total_buffer_chars: int = 1_000_000  # 1M total across all sessions

# manager.py
async def _check_memory_pressure(self) -> None:
    """Clear buffers of largest non-default sessions when total exceeds threshold."""
    total_buffer = 0
    session_buffers: list[tuple[str, int]] = []
    
    for name, session in self._sessions.items():
        buf = getattr(session._backend, '_output_buffer', None)
        if buf is not None:
            size = buf.total_chars if isinstance(buf, SlidingOutputBuffer) else len(buf)
            total_buffer += size
            session_buffers.append((name, size))
    
    if total_buffer <= self._config.max_total_buffer_chars:
        return
    
    session_buffers.sort(key=lambda x: x[1], reverse=True)
    for name, size in session_buffers:
        if name == self._default_terminal:
            continue
        if total_buffer <= self._config.max_total_buffer_chars:
            break
        
        session = self._sessions[name]
        buf = getattr(session._backend, '_output_buffer', None)
        if isinstance(buf, SlidingOutputBuffer):
            buf.clear()
        
        logger.warning(
            "Memory pressure: cleared buffer for '%s' (was %d chars)",
            name, size,
        )
        total_buffer -= size
```

Called from `get_or_create()` and `list_sessions()`.

---

## File Change Summary

| File | Changes |
|------|---------|
| `backends/base.py` | Add `_output_buffer`, `mark_command_boundary()`, `_append_to_buffer()`, move `extract_current_segment_from_buffer()` |
| `backends/visible_windows.py` | Init `SlidingOutputBuffer`, use `_append_to_buffer()`, remove local `extract_current_segment_from_buffer()` |
| `backends/windows_hidden.py` | Init `SlidingOutputBuffer`, use `_append_to_buffer()` |
| `backends/visible_windows_host.py` | Add TODO comment for #4 |
| `results.py` | Add `SlidingOutputBuffer` class |
| `types.py` | Add `TerminalStatus` enum |
| `config.py` | Add `initial_idle_threshold_ms`, `active_idle_threshold_ms`, `max_total_buffer_chars`; remove `input_wait_early_min_elapsed_ms` |
| `process_registry.py` | Tiered threshold in `running_runtime()` |
| `command_tool.py` | Pager detection + auto-scroll, use `submit_command()`, all `_format_*` methods return `<command_result>` XML |
| `process_tool.py` | `log`/`write`/`send_keys`/`list` actions return `<process_result>` XML |
| `session.py` | Add `submit_command()`, call `mark_command_boundary()`, remove PAGER suppression from `_startup_env()` |
| `prompt.py` | Add `detect_pager_entry()`, `_resolve_cursor_line()` |
| `tool.py` | `TerminalAction.CURRENT` returns `<terminal_result>` XML with structured status |
| `manager.py` | Add `_check_memory_pressure()` |

## Non-Goals

- **Task #4 (human-agent detection)**: Deferred. TODO comment only.
- **Task #7 (window title)**: Not implemented.
- **Tmux backend buffer**: Not changed. `_last_capture` is naturally bounded by pane size.
- **ProcessRegistry memory**: Already has bounds (`max_output_chars`, `pending_max_output_chars`). No changes needed.
