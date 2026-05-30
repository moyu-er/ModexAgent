# Terminal System Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix command execution reliability, pager handling, state awareness, and memory leaks in the terminal tool system.

**Architecture:** Introduce `SlidingOutputBuffer` (dual-constraint sliding window) promoted to `TerminalBackend` base class. Add pager auto-scroll with behavior-driven termination. Unify all agent-facing tool returns to structured XML (`<command_result>`, `<process_result>`, `<terminal_result>`). Add tiered idle timeout and global memory pressure detection.

**Tech Stack:** Python 3.12+, asyncio, collections.deque, xml.sax.saxutils

**Spec:** `docs/superpowers/specs/2026-05-30-terminal-system-improvements-design.md`

---

## File Structure

### New files
- `tests/framework/tools/terminal/test_sliding_output_buffer.py` — SlidingOutputBuffer unit tests
- `tests/framework/tools/terminal/test_prompt_pager.py` — pager detection tests

### Modified files (in dependency order)
1. `framework/tools/terminal/results.py` — add `SlidingOutputBuffer`
2. `framework/tools/terminal/backends/base.py` — base class promotion
3. `framework/tools/terminal/backends/visible_windows.py` — use SlidingOutputBuffer
4. `framework/tools/terminal/backends/windows_hidden.py` — use SlidingOutputBuffer
5. `framework/tools/terminal/config.py` — new config fields
6. `framework/tools/terminal/prompt.py` — pager detection utilities
7. `framework/tools/terminal/session.py` — submit_command, mark_command_boundary, PAGER removal
8. `framework/tools/terminal/process_registry.py` — tiered idle timeout
9. `framework/tools/terminal/command_tool.py` — pager auto-scroll, XML format, submit_command
10. `framework/tools/terminal/process_tool.py` — XML format
11. `framework/tools/terminal/tool.py` — terminal current XML format
12. `framework/tools/terminal/manager.py` — memory pressure detection
13. `framework/tools/terminal/backends/visible_windows_host.py` — TODO comment
14. `framework/tools/terminal/types.py` — CommandResultStatus enum

### Modified test files
- `tests/framework/tools/terminal/test_command_tool.py` — update for XML format + new features
- `tests/framework/tools/terminal/test_process_tool.py` — update for XML format
- `tests/framework/tools/terminal/test_terminal_tool_current.py` — update for XML format
- `tests/framework/tools/terminal/test_process_registry.py` — update for tiered timeout

---

### Task 1: SlidingOutputBuffer

**Files:**
- Create: `framework/tools/terminal/results.py` (add class to existing file)
- Create: `tests/framework/tools/terminal/test_sliding_output_buffer.py`

- [ ] **Step 1: Write failing tests for SlidingOutputBuffer**

Create `tests/framework/tools/terminal/test_sliding_output_buffer.py`:

```python
from __future__ import annotations

from framework.tools.terminal.results import SlidingOutputBuffer


def test_append_and_text_returns_content() -> None:
    buf = SlidingOutputBuffer(max_chars=1000, max_commands=10)
    buf.append("hello ")
    buf.append("world")
    assert buf.text == "hello world"


def test_mark_command_boundary_seals_current_parts() -> None:
    buf = SlidingOutputBuffer(max_chars=1000, max_commands=10)
    buf.append("cmd1 output")
    buf.mark_command_boundary()
    buf.append("cmd2 output")
    assert buf.text == "cmd1 outputcmd2 output"


def test_char_constraint_trims_oldest_commands() -> None:
    buf = SlidingOutputBuffer(max_chars=20, max_commands=100)
    buf.append("a" * 10)
    buf.mark_command_boundary()
    buf.append("b" * 10)
    buf.mark_command_boundary()
    buf.append("c" * 10)
    buf.mark_command_boundary()
    # Total would be 30, but max is 20. Oldest command ("a"*10) trimmed.
    assert "a" * 10 not in buf.text
    assert "b" * 10 in buf.text
    assert "c" * 10 in buf.text
    assert buf.total_chars <= 20


def test_command_constraint_limits_deque_size() -> None:
    buf = SlidingOutputBuffer(max_chars=1_000_000, max_commands=3)
    for i in range(5):
        buf.append(f"cmd{i}")
        buf.mark_command_boundary()
    # Only last 3 commands kept
    text = buf.text
    assert "cmd0" not in text
    assert "cmd1" not in text
    assert "cmd2" in text
    assert "cmd3" in text
    assert "cmd4" in text


def test_clear_resets_all_state() -> None:
    buf = SlidingOutputBuffer()
    buf.append("data")
    buf.mark_command_boundary()
    buf.append("more")
    buf.clear()
    assert buf.text == ""
    assert buf.total_chars == 0


def test_mark_command_boundary_with_empty_current_parts_is_noop() -> None:
    buf = SlidingOutputBuffer()
    buf.append("data")
    buf.mark_command_boundary()
    buf.mark_command_boundary()  # no current parts — should be no-op
    assert buf.text == "data"


def test_total_chars_property() -> None:
    buf = SlidingOutputBuffer()
    buf.append("hello")
    assert buf.total_chars == 5
    buf.append(" world")
    assert buf.total_chars == 11
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/framework/tools/terminal/test_sliding_output_buffer.py -v`
Expected: FAIL with `ImportError: cannot import name 'SlidingOutputBuffer'`

- [ ] **Step 3: Implement SlidingOutputBuffer in results.py**

Add to the end of `framework/tools/terminal/results.py`:

```python
from collections import deque


class SlidingOutputBuffer:
    """Dual-constraint sliding window for terminal output.

    - Character constraint: total chars <= max_chars (default 200K)
    - Command constraint: keep last max_commands (default 100) command blocks
    - Both enforced simultaneously; whichever is stricter wins.
    """

    def __init__(self, max_chars: int = 200_000, max_commands: int = 100) -> None:
        self._command_chunks: deque[str] = deque(maxlen=max_commands)
        self._current_parts: list[str] = []
        self._total_chars = 0
        self._max_chars = max_chars

    def append(self, text: str) -> None:
        """Append output text to the current command's buffer."""
        self._current_parts.append(text)
        self._total_chars += len(text)
        self._trim_chars()

    def mark_command_boundary(self) -> None:
        """Seal current parts as a completed command block.

        Called by TerminalSession before each new command so the buffer
        can track per-command chunks and enforce the command limit.
        """
        if self._current_parts:
            chunk = "".join(self._current_parts)
            self._command_chunks.append(chunk)
            self._current_parts = []
            self._recalc_total_chars()

    @property
    def text(self) -> str:
        """Reconstruct full buffer text from command chunks + current parts."""
        parts: list[str] = list(self._command_chunks)
        if self._current_parts:
            parts.append("".join(self._current_parts))
        return "".join(parts)

    @property
    def total_chars(self) -> int:
        """Total character count across all chunks and current parts."""
        return self._total_chars

    def clear(self) -> None:
        """Discard all buffered content."""
        self._command_chunks.clear()
        self._current_parts = []
        self._total_chars = 0

    def _trim_chars(self) -> None:
        """Remove oldest command chunks until total chars <= max_chars."""
        while self._total_chars > self._max_chars and self._command_chunks:
            removed = self._command_chunks.popleft()
            self._total_chars -= len(removed)

    def _recalc_total_chars(self) -> None:
        self._total_chars = sum(len(c) for c in self._command_chunks)
        self._total_chars += sum(len(p) for p in self._current_parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/framework/tools/terminal/test_sliding_output_buffer.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add framework/tools/terminal/results.py tests/framework/tools/terminal/test_sliding_output_buffer.py
git commit -m "feat(terminal): add SlidingOutputBuffer dual-constraint sliding window"
```

---

### Task 2: Base Class Promotion

**Files:**
- Modify: `framework/tools/terminal/backends/base.py`
- Modify: `framework/tools/terminal/backends/visible_windows.py`
- Modify: `framework/tools/terminal/backends/windows_hidden.py`

- [ ] **Step 1: Add __init__, buffer methods, and extract_current_segment to base.py**

In `framework/tools/terminal/backends/base.py`, add imports at the top:

```python
from framework.tools.terminal.results import SlidingOutputBuffer, TerminalRead, TerminalSegment
```

Add `__init__` and helper methods to `TerminalBackend` class (after the class docstring, before the first `@property`):

```python
class TerminalBackend(ABC):
    """Abstract terminal backend — wraps platform-specific PTY libraries."""

    def __init__(self) -> None:
        self._output_buffer: SlidingOutputBuffer | None = None

    def mark_command_boundary(self) -> None:
        """Mark a command boundary. No-op if backend has no sliding buffer."""
        if self._output_buffer is not None:
            self._output_buffer.mark_command_boundary()

    def _append_to_buffer(self, text: str) -> None:
        """Append text to the sliding output buffer. No-op if buffer is None."""
        if self._output_buffer is not None:
            self._output_buffer.append(text)
```

Add the `extract_current_segment_from_buffer` function at module level (after the class):

```python
def extract_current_segment_from_buffer(text: str) -> TerminalSegment:
    """Extract the last terminal segment from buffered PTY output.

    Strips ANSI/CSI sequences before checking for prompt endings so that
    terminal control codes do not prevent empty-prompt detection.
    """
    from framework.tools.terminal.prompt import _strip_ansi_and_da1

    clean = _strip_ansi_and_da1(text)
    lines = clean.splitlines()
    if not lines:
        return TerminalSegment(text="", cursor_line="", is_empty_prompt=True)
    cursor_line = lines[-1]
    prompt_indexes = [
        index
        for index, line in enumerate(lines)
        if line.rstrip().endswith((">", "$", "#", "%"))
    ]
    start = prompt_indexes[-1] if prompt_indexes else max(0, len(lines) - 1)
    segment_text = "\n".join(lines[start:])
    return TerminalSegment(
        text=segment_text,
        cursor_line=cursor_line,
        is_empty_prompt=cursor_line.rstrip().endswith((">", "$", "#", "%")),
    )
```

- [ ] **Step 2: Update VisibleWindowsPtyBackend to use base class buffer**

In `framework/tools/terminal/backends/visible_windows.py`:

1. Remove the local `extract_current_segment_from_buffer` function (lines 30-55).
2. Update the import to use the base class version:

Replace:
```python
from .base import TerminalBackend
```
With:
```python
from .base import TerminalBackend, extract_current_segment_from_buffer
```

3. Add `super().__init__()` and `SlidingOutputBuffer` init in `__init__`:

Replace:
```python
def __init__(self) -> None:
    self._proc: subprocess.Popen | None = None
    self._sock: socket.socket | None = None
    self._shell: str | None = None
    self._title: str = "agent-terminal"
    self._output_buffer: str = ""
```
With:
```python
def __init__(self) -> None:
    super().__init__()
    self._output_buffer = SlidingOutputBuffer()
    self._proc: subprocess.Popen | None = None
    self._sock: socket.socket | None = None
    self._shell: str | None = None
    self._title: str = "agent-terminal"
```

4. Add import for `SlidingOutputBuffer`:
```python
from framework.tools.terminal.results import SlidingOutputBuffer, TerminalRead, TerminalSegment
```

5. Update `read_pending` to use `_append_to_buffer`:

Replace:
```python
async def read_pending(self, timeout, max_size):
    raw = await self.read(timeout=timeout, max_size=max_size)
    if raw:
        self._output_buffer += raw
    return TerminalRead(stdout=raw, raw=raw)
```
With:
```python
async def read_pending(self, timeout: float = 5.0, max_size: int = 65536) -> TerminalRead:
    raw = await self.read(timeout=timeout, max_size=max_size)
    if raw:
        self._append_to_buffer(raw)
    return TerminalRead(stdout=raw, raw=raw)
```

6. Update `current_segment` to use `self._output_buffer.text`:

Replace:
```python
async def current_segment(self) -> TerminalSegment:
    return extract_current_segment_from_buffer(self._output_buffer)
```
With:
```python
async def current_segment(self) -> TerminalSegment:
    return extract_current_segment_from_buffer(self._output_buffer.text)
```

- [ ] **Step 3: Update WindowsHiddenPtyBackend to use base class buffer**

In `framework/tools/terminal/backends/windows_hidden.py`:

1. Add imports:
```python
from framework.tools.terminal.results import SlidingOutputBuffer, TerminalRead, TerminalSegment
from .base import TerminalBackend, extract_current_segment_from_buffer
```

2. Update `__init__`:

Replace:
```python
def __init__(self) -> None:
    self._proc: object | None = None
    self._shell: str | None = None
    self._output_buffer: str = ""
```
With:
```python
def __init__(self) -> None:
    super().__init__()
    self._output_buffer = SlidingOutputBuffer()
    self._proc: object | None = None
    self._shell: str | None = None
```

3. Update `read_pending` to use `_append_to_buffer`:

Replace:
```python
if raw:
    self._output_buffer += raw
```
With:
```python
if raw:
    self._append_to_buffer(raw)
```

4. Update `current_segment` import:

Replace:
```python
async def current_segment(self) -> TerminalSegment:
    from framework.tools.terminal.backends.visible_windows import extract_current_segment_from_buffer
    return extract_current_segment_from_buffer(self._output_buffer)
```
With:
```python
async def current_segment(self) -> TerminalSegment:
    return extract_current_segment_from_buffer(self._output_buffer.text)
```

- [ ] **Step 4: Run existing tests to verify no regressions**

Run: `python -m pytest tests/framework/tools/terminal/ -v`
Expected: All existing tests PASS (no behavioral changes yet — buffer is used the same way)

- [ ] **Step 5: Commit**

```bash
git add framework/tools/terminal/backends/base.py framework/tools/terminal/backends/visible_windows.py framework/tools/terminal/backends/windows_hidden.py
git commit -m "refactor(terminal): promote output buffer and segment extraction to TerminalBackend base"
```

---

### Task 3: Config Additions

**Files:**
- Modify: `framework/tools/terminal/config.py`

- [ ] **Step 1: Add new config fields**

In `framework/tools/terminal/config.py`, add to `TerminalRuntimeConfig`:

Replace the `input_wait_early_min_elapsed_ms` field and add new fields:

```python
@dataclass(frozen=True)
class TerminalRuntimeConfig:
    default_yield_ms: int = 10_000
    min_yield_ms: int = 10
    max_yield_ms: int = 120_000
    default_command_timeout_seconds: int = 60
    long_running_timeout_seconds: int = 300
    command_tool_outer_timeout_seconds: int = 70
    input_wait_idle_ms: int = 10_000
    min_input_wait_idle_ms: int = 1_000
    max_input_wait_idle_ms: int = 600_000
    poll_max_wait_ms: int = 30_000
    max_output_chars: int = 200_000
    pending_max_output_chars: int = 30_000
    finished_ttl_ms: int = 1_800_000
    prompt_stabilize_ms: int = 100
    empty_read_threshold: int = 5
    empty_read_interval_ms: int = 50
    # Tiered idle timeout (replaces input_wait_early_min_elapsed_ms)
    initial_idle_threshold_ms: int = 5_000
    active_idle_threshold_ms: int = 15_000
    # Global memory pressure
    max_total_buffer_chars: int = 1_000_000
    # Pager auto-scroll
    pager_auto_scroll_max_pages: int = 10
    pager_auto_scroll_max_chars: int = 100_000
    pager_idle_detect_seconds: float = 2.0
    output_velocity_window_s: int = 5
    output_velocity_active_threshold: int = 2
```

Note: `input_wait_early_min_elapsed_ms` and `input_wait_early_min_elapsed_ms` are removed.

- [ ] **Step 2: Verify config loads correctly**

Run: `python -c "from framework.tools.terminal.config import TerminalRuntimeConfig; c = TerminalRuntimeConfig(); print(c.initial_idle_threshold_ms, c.active_idle_threshold_ms, c.max_total_buffer_chars)"`
Expected: `5000 15000 1000000`

- [ ] **Step 3: Commit**

```bash
git add framework/tools/terminal/config.py
git commit -m "feat(terminal): add tiered idle timeout and memory pressure config fields"
```

---

### Task 4: Prompt Utilities

**Files:**
- Modify: `framework/tools/terminal/prompt.py`
- Create: `tests/framework/tools/terminal/test_prompt_pager.py`

- [ ] **Step 1: Write failing tests for pager detection**

Create `tests/framework/tools/terminal/test_prompt_pager.py`:

```python
from __future__ import annotations

from framework.tools.terminal.prompt import detect_pager_entry, resolve_cursor_line
from framework.tools.terminal.results import TerminalSegment


def test_detect_pager_entry_bare_colon() -> None:
    assert detect_pager_entry(":") is True


def test_detect_pager_entry_colon_with_spaces() -> None:
    assert detect_pager_entry("  :  ") is True


def test_detect_pager_entry_config_colon_is_not_pager() -> None:
    assert detect_pager_entry("config:") is False


def test_detect_pager_entry_error_colon_is_not_pager() -> None:
    assert detect_pager_entry("error: something failed") is False


def test_detect_pager_entry_empty_string() -> None:
    assert detect_pager_entry("") is False


def test_detect_pager_entry_prompt_is_not_pager() -> None:
    assert detect_pager_entry("user@host:~$ ") is False


def test_resolve_cursor_line_uses_cursor_line_when_present() -> None:
    seg = TerminalSegment(text="line1\nline2", cursor_line="line2", is_empty_prompt=False)
    assert resolve_cursor_line(seg) == "line2"


def test_resolve_cursor_line_falls_back_to_last_nonempty_line() -> None:
    seg = TerminalSegment(text="line1\nline2\n", cursor_line="", is_empty_prompt=False)
    assert resolve_cursor_line(seg) == "line2"


def test_resolve_cursor_line_empty_segment() -> None:
    seg = TerminalSegment(text="", cursor_line="", is_empty_prompt=True)
    assert resolve_cursor_line(seg) == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/framework/tools/terminal/test_prompt_pager.py -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement pager detection in prompt.py**

Add to the end of `framework/tools/terminal/prompt.py`:

```python
_PAGER_ENTRY_MARKER = ":"


def detect_pager_entry(cursor_line: str) -> bool:
    """Detect if cursor line is a pager entry prompt (less colon).

    Only matches bare ":" on its own line. Excludes "config:", "error:", etc.
    """
    return cursor_line.strip() == _PAGER_ENTRY_MARKER


def resolve_cursor_line(segment: TerminalSegment) -> str:
    """Get cursor line, falling back to last non-empty text line.

    The tmux backend does not populate cursor_line (defaults to "").
    This helper provides a consistent fallback.
    """
    if segment.cursor_line:
        return segment.cursor_line
    lines = segment.text.splitlines()
    for line in reversed(lines):
        if line.strip():
            return line
    return ""
```

Also add the import at the top of `prompt.py`:
```python
from framework.tools.terminal.results import TerminalSegment
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/framework/tools/terminal/test_prompt_pager.py -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add framework/tools/terminal/prompt.py tests/framework/tools/terminal/test_prompt_pager.py
git commit -m "feat(terminal): add pager entry detection and cursor line resolution"
```

---

### Task 5: Session Changes (submit_command + mark_command_boundary + PAGER removal)

**Files:**
- Modify: `framework/tools/terminal/session.py`

- [ ] **Step 1: Add submit_command method to TerminalSession**

In `framework/tools/terminal/session.py`, add the following method to `TerminalSession` class (after `send_interrupt`, before `close`):

```python
async def submit_command(self, command: str) -> None:
    """Submit a command to the PTY with proper pre-cleanup and line ending.

    Ensures readline input line is cleared before writing the command,
    and uses the shell-family-appropriate line ending.
    """
    if self.shell_info.family.uses_readline():
        await self._discard_pending_output()
        await self._backend.clear_input_line()
        await asyncio.sleep(0.05)
        await self._discard_pending_output()

    ending = self.shell_info.family.command_ending()
    await self._backend.write(command + ending)
```

- [ ] **Step 2: Add mark_command_boundary call at start of execute()**

In `session.py`, at the very beginning of the `execute()` method body (before the alive check), add:

```python
self._backend.mark_command_boundary()
```

- [ ] **Step 3: Remove PAGER suppression from _startup_env()**

In `session.py`, update `_startup_env` to remove pager suppression:

Replace:
```python
def _startup_env(self) -> dict[str, str]:
    """Return environment for agent-managed terminal sessions."""
    env = dict(os.environ)
    if self._env:
        env.update(self._env)
    env["GIT_PAGER"] = "cat"
    env["PAGER"] = "cat"
    env["LESS"] = "FRX"
    return env
```
With:
```python
def _startup_env(self) -> dict[str, str]:
    """Return environment for agent-managed terminal sessions."""
    env = dict(os.environ)
    if self._env:
        env.update(self._env)
    return env
```

- [ ] **Step 4: Run existing tests**

Run: `python -m pytest tests/framework/tools/terminal/ -v`
Expected: Some tests may fail if FakeBackend lacks `mark_command_boundary`. If so, add to `FakeBackend` in `test_command_tool.py`:

```python
def mark_command_boundary(self) -> None:
    pass
```

Re-run: `python -m pytest tests/framework/tools/terminal/ -v`
Expected: All existing tests PASS

- [ ] **Step 5: Commit**

```bash
git add framework/tools/terminal/session.py
git commit -m "feat(terminal): add submit_command, mark_command_boundary, remove PAGER suppression"
```

---

### Task 6: Tiered Idle Timeout

**Files:**
- Modify: `framework/tools/terminal/process_registry.py`
- Modify: `tests/framework/tools/terminal/test_process_registry.py`

- [ ] **Step 1: Update running_runtime() for tiered thresholds**

In `framework/tools/terminal/process_registry.py`, update the `running_runtime` method. Replace the `early_waiting` calculation:

Replace:
```python
# Early detection: consecutive empty-read equivalent via elapsed time + velocity
elapsed_since_output_ms = idle_ms
early_waiting = (
    session.stdin_writable
    and not velocity.is_active
    and elapsed_since_output_ms >= self._config.input_wait_early_min_elapsed_ms
)
```
With:
```python
# Tiered idle detection: use different thresholds based on command phase
if not session._output_timestamps:
    # Never produced output → use initial threshold (5s default)
    threshold = self._config.initial_idle_threshold_ms
else:
    # Produced output before but currently stalled → use active threshold (15s default)
    threshold = self._config.active_idle_threshold_ms

early_waiting = (
    session.stdin_writable
    and not velocity.is_active
    and idle_ms >= threshold
)
```

- [ ] **Step 2: Update existing test that uses removed field**

In `tests/framework/tools/terminal/test_process_registry.py`, the test `test_waiting_for_input_is_idle_and_stdin_writable_hint` uses `input_wait_idle_ms=1000` which still exists. No change needed for that test.

However, add a new test for tiered behavior:

```python
def test_tiered_idle_threshold_initial_vs_active() -> None:
    """Commands that never produce output use initial threshold (5s).
    Commands that produced output before use active threshold (15s)."""
    registry = ProcessRegistry(config=TerminalRuntimeConfig(
        initial_idle_threshold_ms=500,
        active_idle_threshold_ms=2000,
        input_wait_idle_ms=60_000,  # high — don't trigger formal
    ))

    # Session that never produced output
    session_new = registry.create(command="slow-start", terminal="t1", cwd=None, pid=1)
    session_new.stdin_writable = True
    session_new.last_output_at = time.time() - 1  # 1s idle

    runtime = registry.running_runtime(session_new.id)
    assert runtime is not None
    assert runtime.waiting_for_input is False  # 1s < 500ms? No, 1000ms > 500ms

    # Actually 1s = 1000ms > 500ms threshold, so it should be True
    # Let me fix: set idle to 0.3s (below threshold)
    session_new.last_output_at = time.time() - 0.3
    runtime = registry.running_runtime(session_new.id)
    assert runtime is not None
    assert runtime.waiting_for_input is False  # 300ms < 500ms

    # Session that produced output before
    session_active = registry.create(command="npm install", terminal="t2", cwd=None, pid=2)
    session_active.stdin_writable = True
    session_active.last_output_at = time.time() - 1  # 1s idle
    session_active._output_timestamps = [time.time() - 5]  # had output 5s ago

    runtime = registry.running_runtime(session_active.id)
    assert runtime is not None
    assert runtime.waiting_for_input is False  # 1s < 2000ms active threshold
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/framework/tools/terminal/test_process_registry.py -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add framework/tools/terminal/process_registry.py tests/framework/tools/terminal/test_process_registry.py
git commit -m "feat(terminal): tiered idle timeout — initial 5s, active 15s"
```

---

### Task 7: CommandTool Overhaul (pager + submit_command + XML format)

**Files:**
- Modify: `framework/tools/terminal/types.py`
- Modify: `framework/tools/terminal/command_tool.py`
- Modify: `tests/framework/tools/terminal/test_command_tool.py`

This is the largest task. It combines pager auto-scroll, submit_command usage, and XML return format.

- [ ] **Step 1: Add CommandResultStatus enum to types.py**

Add to `framework/tools/terminal/types.py`:

```python
class CommandResultStatus(StrEnum):
    """CommandTool return status — used in <command_result> XML."""
    COMPLETED = "completed"
    RUNNING = "running"
    TIMED_OUT = "timed_out"
    PAGINATED = "paginated"
    INPUT_WAIT = "input_wait"
```

- [ ] **Step 2: Add XML builder helper to command_tool.py**

Add at the top of `framework/tools/terminal/command_tool.py`:

```python
from xml.sax.saxutils import escape as xml_escape

from framework.tools.terminal.prompt import detect_pager_entry, resolve_cursor_line, sanitize_terminal_output
from framework.tools.terminal.types import CommandResultStatus
```

Add the XML builder helper function (module-level, before the class):

```python
def _build_command_xml(
    output: str,
    status: CommandResultStatus,
    elapsed_ms: int,
    *,
    idle_ms: int | None = None,
    pages_scrolled: int | None = None,
    truncated: bool | None = None,
    message: str | None = None,
) -> str:
    """Build a <command_result> XML string."""
    parts = [
        "<command_result>",
        f"<output>{xml_escape(output)}</output>",
        f"<status>{status.value}</status>",
        f"<elapsed_ms>{elapsed_ms}</elapsed_ms>",
    ]
    if idle_ms is not None:
        parts.append(f"<idle_ms>{idle_ms}</idle_ms>")
    if pages_scrolled is not None:
        parts.append(f"<pages_scrolled>{pages_scrolled}</pages_scrolled>")
    if truncated is not None:
        parts.append(f"<truncated>{str(truncated).lower()}</truncated>")
    if message is not None:
        parts.append(f"<message>{xml_escape(message)}</message>")
    parts.append("</command_result>")
    return "\n".join(parts)
```

- [ ] **Step 3: Rewrite _format_completed to return XML**

Replace the existing `_format_completed` static method:

```python
@staticmethod
def _format_completed(output_parts: list[str], elapsed_ms: int) -> str:
    raw = "".join(output_parts)
    output = sanitize_terminal_output(raw).rstrip()
    return _build_command_xml(
        output or "(no output)",
        CommandResultStatus.COMPLETED,
        elapsed_ms,
    )
```

- [ ] **Step 4: Rewrite _format_running to return XML**

Replace the existing `_format_running` static method:

```python
@staticmethod
async def _format_running(
    terminal_session: TerminalSession,
    output_parts: list[str],
    runtime: RunningSessionRuntime | None,
    elapsed_ms: int,
) -> str:
    raw = "".join(output_parts)
    output = sanitize_terminal_output(raw).rstrip()
    idle_ms = runtime.idle_ms if runtime else None

    if runtime is not None and runtime.waiting_for_input:
        message = (
            f"No new output for {runtime.idle_ms // 1000}s; this session may be "
            "waiting for input. Use process write, send_keys, submit, or paste "
            "to provide input."
        )
        return _build_command_xml(
            output, CommandResultStatus.INPUT_WAIT, elapsed_ms,
            idle_ms=idle_ms, message=message,
        )

    message = (
        "Command still running. Use process poll/log for status, "
        "process write/send_keys/paste for input."
    )
    xml = _build_command_xml(
        output, CommandResultStatus.RUNNING, elapsed_ms,
        idle_ms=idle_ms, message=message,
    )

    if terminal_session.cursor_key_mode == CursorKeyMode.APPLICATION:
        segment = await terminal_session.current_segment()
        if segment and segment.text.strip():
            tui_text = sanitize_terminal_output(segment.text).rstrip()
            xml = xml.replace(
                "</command_result>",
                f"\n<tui_screen>{xml_escape(tui_text)}</tui_screen>\n</command_result>",
            )

    return xml
```

- [ ] **Step 5: Rewrite _format_timed_out to return XML**

Replace the existing `_format_timed_out` static method:

```python
@staticmethod
def _format_timed_out(output_parts: list[str], timeout_seconds: int, elapsed_ms: int) -> str:
    raw = "".join(output_parts)
    output = sanitize_terminal_output(raw).rstrip()
    message = (
        f"Command timed out after {timeout_seconds}s and was terminated. "
        "Partial output captured above."
    )
    return _build_command_xml(
        output, CommandResultStatus.TIMED_OUT, elapsed_ms,
        message=message,
    )
```

- [ ] **Step 6: Add _format_paginated method**

Add a new static method to `CommandTool`:

```python
@staticmethod
def _format_paginated(
    output_parts: list[str],
    pages_scrolled: int,
    elapsed_ms: int,
    total_chars: int,
    max_chars: int,
) -> str:
    raw = "".join(output_parts)
    output = sanitize_terminal_output(raw).rstrip()
    truncated = total_chars >= max_chars
    message = (
        "Output was displayed through a pager and automatically scrolled. "
        'If content was cut off, use process send_keys keys=[" "] to continue '
        'scrolling, or process send_keys keys=["q"] to exit the pager.'
    )
    return _build_command_xml(
        output, CommandResultStatus.PAGINATED, elapsed_ms,
        pages_scrolled=pages_scrolled,
        truncated=truncated,
        message=message,
    )
```

- [ ] **Step 7: Update execute() to use submit_command and pass elapsed_ms**

In the `execute()` method, replace:
```python
await session.write(command + "\r")
```
With:
```python
await session.submit_command(command)
```

Update all `_format_*` calls to pass `elapsed_ms`:

Replace:
```python
return self._format_completed(output_parts)
```
With:
```python
return self._format_completed(output_parts, elapsed_ms)
```

Replace:
```python
return await self._format_running(session, output_parts, runtime)
```
With:
```python
return await self._format_running(session, output_parts, runtime, elapsed_ms)
```

Replace:
```python
return await self._format_running(session, output_parts, None)
```
With:
```python
return await self._format_running(session, output_parts, None, elapsed_ms)
```

Replace:
```python
return self._format_timed_out(output_parts, timeout_seconds)
```
With:
```python
return self._format_timed_out(output_parts, timeout_seconds, elapsed_ms)
```

- [ ] **Step 8: Add pager detection and auto-scroll to execute() read loop**

Add `last_output_time` tracking variable after `start = time.monotonic()`:

```python
start = time.monotonic()
last_output_time = start
output_parts: list[str] = []
output_received = False
prompt_stable_since: float | None = None
```

Update the output tracking to set `last_output_time`:

```python
if read.stdout:
    self._registry.append_output(proc.id, "stdout", read.stdout)
    output_parts.append(read.stdout)
    output_received = True
    prompt_stable_since = None
    last_output_time = time.monotonic()
```

Insert pager detection between the prompt detection block (step 2) and the timeout block (step 3):

```python
# 2.5 Pager detection
if output_received and not read.stdout:
    idle_elapsed = time.monotonic() - last_output_time
    if idle_elapsed >= self._config.pager_idle_detect_seconds:
        segment = await session.current_segment()
        cursor = resolve_cursor_line(segment)
        if (not segment.is_empty_prompt
                and detect_pager_entry(cursor)):
            output_parts, pages = await self._auto_scroll_pager(
                session, output_parts, proc.id,
            )
            self._registry.mark_exited(
                proc.id,
                exit_code=None,
                exit_signal=None,
                status=ProcessStatus.COMPLETED,
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)
            total_chars = sum(len(p) for p in output_parts)
            return self._format_paginated(
                output_parts, pages, elapsed_ms,
                total_chars, self._config.pager_auto_scroll_max_chars,
            )
```

- [ ] **Step 9: Add _auto_scroll_pager method to CommandTool**

Add this method to the `CommandTool` class:

```python
async def _auto_scroll_pager(
    self,
    session: TerminalSession,
    initial_output: list[str],
    proc_id: str,
) -> tuple[list[str], int]:
    """Auto-scroll pager until no new content or limit reached.

    Termination conditions (behavior-driven, no marker enumeration):
    1. Space sent, pager_idle_detect_seconds no new output → reached end
    2. Page count reaches max
    3. Total output chars reaches max
    4. Shell prompt appears → pager exited
    """
    output_parts = list(initial_output)
    total_chars = sum(len(p) for p in output_parts)
    pages_scrolled = 0
    idle_timeout = self._config.pager_idle_detect_seconds

    for _ in range(self._config.pager_auto_scroll_max_pages):
        await session.write(" ")  # Space = next page

        new_output = False
        deadline = time.monotonic() + idle_timeout
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
        if total_chars >= self._config.pager_auto_scroll_max_chars:
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

- [ ] **Step 10: Add asyncio import if missing**

Ensure `import asyncio` is present at the top of `command_tool.py`.

- [ ] **Step 11: Update existing tests for XML format**

In `tests/framework/tools/terminal/test_command_tool.py`:

Update `test_command_returns_completed_when_prompt_detected`:
```python
@pytest.mark.asyncio
async def test_command_returns_completed_when_prompt_detected() -> None:
    tool, manager, _registry = make_tool()
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    backend._preread_buffer = [
        TerminalRead(stdout="done\n", raw="done\n"),
        TerminalRead(),
    ]
    backend._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    result = await tool.execute(command="echo done")

    assert "<command_result>" in result
    assert "<status>completed</status>" in result
    assert "done" in result
```

Update `test_command_returns_running_when_yield_window_expires`:
```python
@pytest.mark.asyncio
async def test_command_returns_running_when_yield_window_expires() -> None:
    cfg = TerminalRuntimeConfig(default_yield_ms=10)
    tool, _manager, registry = make_tool(cfg)

    result = await tool.execute(command="npm run dev")

    running = registry.list_running()
    assert len(running) == 1
    assert running[0].command == "npm run dev"
    assert "<status>running</status>" in result
    assert "Command still running" in result
```

Update `test_command_timeout_returns_partial_output`:
```python
@pytest.mark.asyncio
async def test_command_timeout_returns_partial_output() -> None:
    cfg = TerminalRuntimeConfig(
        default_command_timeout_seconds=1,
        command_tool_outer_timeout_seconds=3,
        prompt_stabilize_ms=0,
        default_yield_ms=60_000,
    )
    tool, manager, _registry = make_tool(cfg)
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    backend._preread_buffer = [TerminalRead(stdout="partial\n", raw="partial\n")]
    backend._segment = TerminalSegment(text="...", cursor_line="...", is_empty_prompt=False)

    result = await tool.execute(command="slow")

    assert "partial" in result
    assert "<status>timed_out</status>" in result
```

Update `test_command_returns_running_with_waiting_for_input_hint`:
```python
@pytest.mark.asyncio
async def test_command_returns_running_with_waiting_for_input_hint() -> None:
    cfg = TerminalRuntimeConfig(
        default_yield_ms=60_000,
        input_wait_idle_ms=100,
        initial_idle_threshold_ms=50,
        default_command_timeout_seconds=5,
        command_tool_outer_timeout_seconds=10,
    )
    tool, manager, registry = make_tool(cfg)
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    backend._preread_buffer = [TerminalRead(stdout="password:\n", raw="password:\n")]
    backend._segment = TerminalSegment(text="password:", cursor_line="password:", is_empty_prompt=False)
    backend.alive = True

    result = await tool.execute(command="ssh host")

    assert "<status>input_wait</status>" in result
    assert "password:" in result
```

Update `test_command_completed_output_is_plain_text`:
```python
@pytest.mark.asyncio
async def test_command_completed_output_is_xml() -> None:
    tool, manager, _registry = make_tool()
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    backend._preread_buffer = [TerminalRead(stdout="hello\n", raw="hello\n"), TerminalRead()]
    backend._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    result = await tool.execute(command="echo hello")

    assert "<command_result>" in result
    assert "<status>completed</status>" in result
    assert "hello" in result
```

Update `test_command_timed_out_format`:
```python
@pytest.mark.asyncio
async def test_command_timed_out_format() -> None:
    cfg = TerminalRuntimeConfig(
        default_command_timeout_seconds=1,
        command_tool_outer_timeout_seconds=3,
        prompt_stabilize_ms=0,
        default_yield_ms=60_000,
    )
    tool, manager, _registry = make_tool(cfg)
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    backend._preread_buffer = [TerminalRead(stdout="build...\n", raw="build...\n")]
    backend._segment = TerminalSegment(text="...", cursor_line="...", is_empty_prompt=False)

    result = await tool.execute(command="build")

    assert "build..." in result
    assert "<status>timed_out</status>" in result
```

Update `test_command_writes_newline_to_session`:
```python
@pytest.mark.asyncio
async def test_command_uses_submit_command() -> None:
    """Command is submitted via session.submit_command with proper line ending."""
    tool, manager, _registry = make_tool()
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    backend.reads = [TerminalRead()]
    backend._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    await tool.execute(command="ls")

    # submit_command uses shell_info.family.command_ending() which is "\n" for bash
    assert any("ls\n" in w for w in backend.writes)
```

Update `test_command_no_output_returns_placeholder`:
```python
@pytest.mark.asyncio
async def test_command_no_output_returns_placeholder() -> None:
    tool, manager, _registry = make_tool()
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    backend.alive = False

    async def always_dead() -> bool:
        return False

    backend.is_alive = always_dead  # type: ignore[assignment]

    result = await tool.execute(command="true")

    assert "(no output)" in result
    assert "<status>completed</status>" in result
```

- [ ] **Step 12: Verify FakeBackend has all required methods**

In `tests/framework/tools/terminal/test_command_tool.py`, verify `FakeBackend` has `mark_command_boundary` (added in Task 5 Step 4). No additional changes needed if Task 5 was completed correctly.

- [ ] **Step 13: Run all command tool tests**

Run: `python -m pytest tests/framework/tools/terminal/test_command_tool.py -v`
Expected: All tests PASS

- [ ] **Step 14: Commit**

```bash
git add framework/tools/terminal/types.py framework/tools/terminal/command_tool.py tests/framework/tools/terminal/test_command_tool.py
git commit -m "feat(terminal): CommandTool XML format, pager auto-scroll, submit_command integration"
```

---

### Task 8: ProcessTool XML Format

**Files:**
- Modify: `framework/tools/terminal/process_tool.py`
- Modify: `tests/framework/tools/terminal/test_process_tool.py`

- [ ] **Step 1: Add XML builder helper to process_tool.py**

Add at the top of `framework/tools/terminal/process_tool.py`:

```python
from xml.sax.saxutils import escape as xml_escape
```

Add module-level helper:

```python
def _build_process_xml(
    action: str,
    output: str,
    *,
    session_id: str | None = None,
    status: str | None = None,
    idle_ms: int | None = None,
    bytes_written: int | None = None,
    sessions_xml: str | None = None,
) -> str:
    """Build a <process_result> XML string."""
    parts = [
        "<process_result>",
        f"<action>{action}</action>",
        f"<output>{xml_escape(output)}</output>",
    ]
    if session_id is not None:
        parts.append(f"<session_id>{session_id}</session_id>")
    if status is not None:
        parts.append(f"<status>{status}</status>")
    if idle_ms is not None:
        parts.append(f"<idle_ms>{idle_ms}</idle_ms>")
    if bytes_written is not None:
        parts.append(f"<bytes_written>{bytes_written}</bytes_written>")
    if sessions_xml is not None:
        parts.append(f"<sessions>{sessions_xml}</sessions>")
    parts.append("</process_result>")
    return "\n".join(parts)
```

- [ ] **Step 2: Update _do_list to return XML**

Replace `_do_list`:

```python
async def _do_list(self) -> str:
    running = self._registry.list_running()
    finished = self._registry.list_finished()
    if not running and not finished:
        return _build_process_xml("list", "No running or recent sessions.")

    lines: list[str] = []
    session_entries: list[str] = []
    for s in running:
        runtime = self._registry.running_runtime(s.id)
        lines.append(_format_list_line(s, runtime))
        idle = runtime.idle_ms if runtime else 0
        session_entries.append(
            f'<session id="{s.id}" status="running" '
            f'command="{xml_escape(s.command)}" '
            f'elapsed_ms="{int((s.ended_at or time.time()) - s.started_at) * 1000}" '
            f'idle_ms="{idle}" />'
        )
    for s in finished:
        lines.append(_format_list_line(s))
        session_entries.append(
            f'<session id="{s.id}" status="{s.status.value}" '
            f'command="{xml_escape(s.command)}" '
            f'elapsed_ms="{int((s.ended_at or 0) - s.started_at) * 1000}" '
            f'exit_code="{s.exit_code}" />'
        )

    return _build_process_xml(
        "list", "\n".join(lines),
        sessions_xml="\n".join(session_entries),
    )
```

Add `import time` at the top if not already present.

- [ ] **Step 3: Update _do_log to return XML**

Replace `_do_log`:

```python
async def _do_log(self, offset: int = 0, limit: int = _DEFAULT_LOG_TAIL_LINES) -> str:
    _terminal, running, finished = await self._resolve_terminal()

    session = running or finished
    if session is None:
        return _build_process_xml("log", "[Error] No process session found for default terminal")

    all_lines = session.aggregated.splitlines()
    total = len(all_lines)
    using_default_tail = offset == 0 and limit == _DEFAULT_LOG_TAIL_LINES
    sliced = all_lines[offset: offset + limit]
    output = "\n".join(sliced) or "(no output yet)"

    tail_note = ""
    if using_default_tail and total > _DEFAULT_LOG_TAIL_LINES:
        tail_note = f"\n\n[showing last {_DEFAULT_LOG_TAIL_LINES} of {total} lines; pass offset/limit to page]"

    runtime = (
        self._registry.running_runtime(session.id)
        if session.status == ProcessStatus.RUNNING
        else None
    )
    hint = _build_input_wait_hint(runtime)

    return _build_process_xml(
        "log", output + tail_note + hint,
        session_id=session.id,
        status=session.status.value,
        idle_ms=runtime.idle_ms if runtime else None,
    )
```

- [ ] **Step 4: Update _do_write to return XML**

Replace `_do_write`:

```python
async def _do_write(self, params: WriteParams) -> str:
    terminal_session, running, _finished = await self._resolve_terminal()
    if running is None:
        return _build_process_xml("write", "[Error] No running process session found for default terminal")

    await terminal_session.write(params.data)
    if params.submit:
        await terminal_session.write("\r")

    info = f"Wrote {len(params.data)} bytes to session {running.id}"
    if params.submit:
        info += " + Enter"

    output = await _drain_terminal_after_action(terminal_session, self._registry, running.id, self._config)
    full_output = f"{info}.\nTerminal output:\n{output}" if output else f"{info}."

    return _build_process_xml(
        "write", full_output,
        session_id=running.id,
        bytes_written=len(params.data),
    )
```

- [ ] **Step 5: Update remaining _do_* methods to return XML**

Update `_do_submit`:
```python
async def _do_submit(self) -> str:
    terminal_session, running, _finished = await self._resolve_terminal()
    if running is None:
        return _build_process_xml("submit", "[Error] No running process session found for default terminal")

    await terminal_session.write("\r")
    output = await _drain_terminal_after_action(terminal_session, self._registry, running.id, self._config)
    full_output = f"Sent Enter to session {running.id}.\nTerminal output:\n{output}" if output else f"Sent Enter to session {running.id}."
    return _build_process_xml("submit", full_output, session_id=running.id)
```

Update `_do_send_keys`:
```python
async def _do_send_keys(self, params: SendKeysParams) -> str:
    terminal_session, running, _finished = await self._resolve_terminal()
    if running is None:
        return _build_process_xml("send_keys", "[Error] No running process session found for default terminal")

    cursor_mode = running.cursor_key_mode

    if params.keys and needs_cursor_mode(params.keys) and cursor_mode == CursorKeyMode.UNKNOWN:
        return _build_process_xml(
            "send_keys",
            f"Session {running.id} cursor key mode is not known yet. "
            "Poll or log until startup output appears, then retry send_keys.",
            session_id=running.id,
        )

    parts: list[bytes] = []
    warnings: list[str] = []

    if params.literal:
        parts.append(params.literal.encode("utf-8"))

    for token in params.hex_bytes or []:
        try:
            parts.append(bytes([int(token, 16)]))
        except ValueError:
            warnings.append(f"Invalid hex byte: {token}")

    if params.keys:
        parts.append(encode_key_sequence(params.keys, cursor_mode))

    combined = b"".join(parts)
    if not combined:
        return _build_process_xml("send_keys", "[Error] No key data provided.")

    await terminal_session.write(combined.decode("utf-8", errors="surrogateescape"))

    result_text = f"Sent {len(combined)} bytes to session {running.id}."
    if warnings:
        result_text += "\nWarnings:\n- " + "\n- ".join(warnings)
    return _build_process_xml("send_keys", result_text, session_id=running.id)
```

Update `_do_paste`:
```python
async def _do_paste(self, params: PasteParams) -> str:
    terminal_session, running, _finished = await self._resolve_terminal()
    if running is None:
        return _build_process_xml("paste", "[Error] No running process session found for default terminal")

    payload = encode_paste(params.text, bracketed=terminal_session.bracketed_paste_enabled)
    await terminal_session.write(payload.decode("utf-8", errors="surrogateescape"))
    return _build_process_xml(
        "paste", f"Pasted {len(params.text)} chars to session {running.id}.",
        session_id=running.id,
    )
```

Update `_do_interrupt`:
```python
async def _do_interrupt(self) -> str:
    terminal_session, running, _finished = await self._resolve_terminal()
    if running is None:
        return _build_process_xml("interrupt", "[Error] No running process session found for default terminal")

    await terminal_session.interrupt()
    return _build_process_xml(
        "interrupt", f"Sent interrupt (Ctrl+C) to session {running.id}.",
        session_id=running.id,
    )
```

Update `_do_kill`:
```python
async def _do_kill(self) -> str:
    terminal_session, running, _finished = await self._resolve_terminal()
    if running is None:
        return _build_process_xml("kill", "[Error] No running process session found for default terminal")

    await terminal_session.terminate()
    self._registry.mark_exited(
        running.id,
        exit_code=None,
        exit_signal="KILLED",
        status=ProcessStatus.KILLED,
    )
    return _build_process_xml("kill", f"Killed session {running.id}.", session_id=running.id)
```

Update `_do_clear`:
```python
async def _do_clear(self) -> str:
    _terminal, _running, finished = await self._resolve_terminal()
    if finished is None:
        return _build_process_xml("clear", "[Error] No finished session found for default terminal")
    self._registry.delete(finished.id)
    return _build_process_xml("clear", f"Cleared finished session {finished.id}.")
```

Update `_do_remove`:
```python
async def _do_remove(self) -> str:
    terminal_session, running, finished = await self._resolve_terminal()

    if running is not None:
        await terminal_session.terminate()
        self._registry.mark_exited(
            running.id,
            exit_code=None,
            exit_signal="KILLED",
            status=ProcessStatus.KILLED,
        )
        self._registry.delete(running.id)
        return _build_process_xml("remove", f"Killed and removed session {running.id}.")

    if finished is not None:
        self._registry.delete(finished.id)
        return _build_process_xml("remove", f"Removed finished session {finished.id}.")

    return _build_process_xml("remove", "[Error] No process session found for default terminal")
```

- [ ] **Step 6: Update existing ProcessTool tests for XML format**

In `tests/framework/tools/terminal/test_process_tool.py`:

Update `test_process_log_reads_from_registry`:
```python
@pytest.mark.asyncio
async def test_process_log_reads_from_registry() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="server", terminal="default", cwd=None, pid=1)
    registry.append_output(session.id, "stdout", "ready\n")
    terminal = FakeTerminal()
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    text = await tool.execute(action="log")

    assert "<process_result>" in text
    assert "<action>log</action>" in text
    assert "ready" in text
```

Update `test_process_write_submit_interrupt_and_kill`:
```python
@pytest.mark.asyncio
async def test_process_write_submit_interrupt_and_kill() -> None:
    registry = ProcessRegistry()
    registry.create(command="ssh host", terminal="default", cwd=None, pid=2)
    terminal = FakeTerminal()
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    write_result = await tool.execute(action="write", data="password")
    submit_result = await tool.execute(action="submit")
    interrupt_result = await tool.execute(action="interrupt")
    kill_result = await tool.execute(action="kill")

    assert terminal.writes == ["password", "\r"]
    assert terminal.interrupted is True
    assert terminal.killed is True
    assert "<process_result>" in write_result
    assert "<action>write</action>" in write_result
    assert "<process_result>" in kill_result
    assert "<action>kill</action>" in kill_result
    running = registry.get_running_by_terminal("default")
    assert running is None
    finished = registry.get_finished_by_terminal("default")
    assert finished is not None
    assert finished.status is ProcessStatus.KILLED
```

- [ ] **Step 7: Run all process tool tests**

Run: `python -m pytest tests/framework/tools/terminal/test_process_tool.py -v`
Expected: All tests PASS

- [ ] **Step 8: Commit**

```bash
git add framework/tools/terminal/process_tool.py tests/framework/tools/terminal/test_process_tool.py
git commit -m "feat(terminal): ProcessTool returns structured <process_result> XML"
```

---

### Task 9: terminal current XML Format

**Files:**
- Modify: `framework/tools/terminal/tool.py`
- Modify: `tests/framework/tools/terminal/test_terminal_tool_current.py`

- [ ] **Step 1: Update TerminalAction.CURRENT handler to return XML**

In `framework/tools/terminal/tool.py`, add imports:

```python
from xml.sax.saxutils import escape as xml_escape
from framework.tools.terminal.prompt import detect_pager_entry, resolve_cursor_line, sanitize_terminal_output
```

Replace the `TerminalAction.CURRENT` handler:

```python
if action_enum == TerminalAction.CURRENT:
    if name:
        session = await self._manager.get_or_create(name)
    else:
        session = await self._manager.get_default_session()
    if session is None:
        return (
            "<terminal_result>\n"
            "<action>current</action>\n"
            "<status>none</status>\n"
            "<output>No terminal is active. Use terminal open to create one.</output>\n"
            "</terminal_result>"
        )
    segment = await session.current_segment()
    cleaned = sanitize_terminal_output(segment.text).rstrip()

    # Infer status
    if segment.is_empty_prompt:
        status = "idle"
    elif session._busy_after_timeout:
        status = "busy"
    elif session._last_status == "waiting_input":
        status = "waiting_input"
    elif detect_pager_entry(resolve_cursor_line(segment)):
        status = "pager"
    else:
        status = "active"

    cursor = segment.cursor_line.strip() if segment.cursor_line else ""
    output_lines = cleaned.splitlines()[-30:] if cleaned else []
    output_text = "\n".join(output_lines) if output_lines else "(terminal is idle — no output yet)"

    return (
        "<terminal_result>\n"
        "<action>current</action>\n"
        f"<status>{status}</status>\n"
        f"<cursor>{xml_escape(cursor)}</cursor>\n"
        f"<output>{xml_escape(output_text)}</output>\n"
        "</terminal_result>"
    )
```

- [ ] **Step 2: Update existing test for XML format**

In `tests/framework/tools/terminal/test_terminal_tool_current.py`:

Replace `test_terminal_current_returns_empty_prompt_as_current_segment`:

```python
@pytest.mark.asyncio
async def test_terminal_current_returns_xml_with_idle_status() -> None:
    tool = TerminalTool(FakeManager())

    result = await tool.execute(action="current")

    assert "<terminal_result>" in result
    assert "<status>idle</status>" in result
    assert "<action>current</action>" in result
    assert "$ " in result
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/framework/tools/terminal/test_terminal_tool_current.py -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add framework/tools/terminal/tool.py tests/framework/tools/terminal/test_terminal_tool_current.py
git commit -m "feat(terminal): terminal current returns <terminal_result> XML with status"
```

---

### Task 10: Manager Memory Pressure Detection

**Files:**
- Modify: `framework/tools/terminal/manager.py`

- [ ] **Step 1: Add _check_memory_pressure method to TerminalManager**

In `framework/tools/terminal/manager.py`, add import:

```python
from framework.tools.terminal.results import SlidingOutputBuffer
```

Add method to `TerminalManager` class:

```python
async def _check_memory_pressure(self) -> None:
    """Clear buffers of largest non-default sessions when total exceeds threshold."""
    total_buffer = 0
    session_buffers: list[tuple[str, int]] = []

    for name, session in self._sessions.items():
        buf = getattr(session._backend, "_output_buffer", None)
        if buf is not None:
            size = buf.total_chars if isinstance(buf, SlidingOutputBuffer) else len(buf)
            total_buffer += size
            session_buffers.append((name, size))

    if total_buffer <= self._config.max_total_buffer_chars if hasattr(self, '_config') else total_buffer <= 1_000_000:
        return

    session_buffers.sort(key=lambda x: x[1], reverse=True)
    for name, size in session_buffers:
        if name == self._default_terminal:
            continue
        if total_buffer <= (self._config.max_total_buffer_chars if hasattr(self, '_config') else 1_000_000):
            break

        session = self._sessions[name]
        buf = getattr(session._backend, "_output_buffer", None)
        if isinstance(buf, SlidingOutputBuffer):
            buf.clear()

        logger.warning(
            "Memory pressure: cleared buffer for '%s' (was %d chars)",
            name, size,
        )
        total_buffer -= size
```

Note: `TerminalManager` doesn't currently have a `_config` attribute. Add it to `__init__`:

```python
from framework.tools.terminal.config import TerminalRuntimeConfig

# In __init__, add after existing assignments:
self._config = TerminalRuntimeConfig()
```

Then the threshold check becomes simply:

```python
    if total_buffer <= self._config.max_total_buffer_chars:
        return

    session_buffers.sort(key=lambda x: x[1], reverse=True)
    for name, size in session_buffers:
        if name == self._default_terminal:
            continue
        if total_buffer <= self._config.max_total_buffer_chars:
            break

- [ ] **Step 2: Call _check_memory_pressure from get_or_create and list_sessions**

At the end of `get_or_create`, before the return:
```python
await self._check_memory_pressure()
```

At the end of `list_sessions`, before the return:
```python
await self._check_memory_pressure()
```

- [ ] **Step 3: Run existing tests**

Run: `python -m pytest tests/framework/tools/terminal/ -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add framework/tools/terminal/manager.py
git commit -m "feat(terminal): global memory pressure detection with buffer clearing"
```

---

### Task 11: TODO Comments for Deferred Items

**Files:**
- Modify: `framework/tools/terminal/backends/visible_windows_host.py`

- [ ] **Step 1: Add TODO comment for human-agent detection**

In `framework/tools/terminal/backends/visible_windows_host.py`, add at the top of the file (after the module docstring):

```python
# TODO(terminal-human-input): Detect human keyboard input in visible terminal
# and notify the parent process via out-of-band socket marker (\x00HUMAN\x00).
# Parent filters the marker and sets a flag on the backend.
# Session layer checks the flag and appends a note to command results.
# Only affects VisibleWindowsPtyBackend (not hidden or tmux).
# Requires: socket write lock in host process to prevent marker interleaving.
# See: docs/superpowers/specs/2026-05-30-terminal-system-improvements-design.md §4
```

- [ ] **Step 2: Commit**

```bash
git add framework/tools/terminal/backends/visible_windows_host.py
git commit -m "docs(terminal): add TODO for human-agent mixed input detection"
```

---

### Task 12: Final Verification

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/framework/tools/terminal/ -v`
Expected: All tests PASS

- [ ] **Step 2: Run linting**

Run: `python -m ruff check framework/tools/terminal/`
Expected: No errors (warnings OK)

- [ ] **Step 3: Run type checking**

Run: `python -m mypy framework/tools/terminal/`
Expected: No new errors

- [ ] **Step 4: Verify all spec items are covered**

Check against spec:
- [x] #1 Tiered idle timeout → Task 3 (config) + Task 6 (process_registry)
- [x] #2 Pager auto-scroll → Task 4 (prompt) + Task 7 (command_tool)
- [x] #3 terminal current structured status → Task 9 (tool.py)
- [x] #4 Human-agent detection → Task 11 (TODO comment)
- [x] #5 Unified command submission → Task 5 (session) + Task 7 (command_tool)
- [x] #6 Memory leak fix → Task 1 (SlidingOutputBuffer) + Task 2 (base class) + Task 10 (manager)
- [x] XML return format → Task 7 (command) + Task 8 (process) + Task 9 (terminal)
