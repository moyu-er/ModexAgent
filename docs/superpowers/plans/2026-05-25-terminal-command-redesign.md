# Terminal Command Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current `ShellTool`/subprocess-style execution path with a Windows-first OpenClaw-inspired `command` + `process` + `terminal` system that supports visible and hidden PTY-backed terminal tabs through one tool-facing abstraction.

**Architecture:** Tools depend only on a `TerminalManager` protocol and a `ProcessRegistry`; visible vs hidden behavior is selected by concrete Windows manager/backend implementations. `command` starts commands and may return a running `session_id`; `process` handles follow-up polling, logs, input, interrupt, and termination; `terminal` manages named tabs and exposes `current` terminal state.

**Tech Stack:** Python 3.12, existing `framework.core.tool_manager.Tool`, `framework.tools.terminal`, `pywinpty`/Windows PTY host code, pytest, existing bot_project pool builder and IOC config models.

---

## Reference Context

Read these before implementation:

- `docs/superpowers/specs/2026-05-25-terminal-command-redesign.md`
- `framework/tools/terminal/AGENTS.md`
- `framework/tools/standard/shell_tool.py`
- `framework/tools/terminal/session.py`
- `framework/tools/terminal/manager.py`
- `framework/tools/terminal/backends/base.py`
- `framework/tools/terminal/backends/visible_windows.py`
- `framework/tools/terminal/backends/visible_windows_host.py`
- `examples/bot_project/bot/service/pool_builder.py`
- `examples/bot_project/bot/service/builders.py`
- `examples/bot_project/bot/service/core.py`
- OpenClaw references:
  - `references/openclaw/src/agents/bash-tools.exec.ts`
  - `references/openclaw/src/agents/bash-tools.process.ts`
  - `references/openclaw/src/agents/bash-process-registry.ts`
  - `references/openclaw/src/agents/bash-tools.process-send-keys.ts`

## Target File Structure

Create or modify these files:

- Create `framework/tools/terminal/config.py`: typed defaults, clamping helpers, manager kind config.
- Modify `framework/tools/terminal/types.py`: add `POWERSHELL`, `TerminalVisibility`, result/status dataclasses, and typed key/action enums.
- Create `framework/tools/terminal/results.py`: `CommandResult`, `ProcessActionResult`, `TerminalSegment`, `TerminalRead`.
- Create `framework/tools/terminal/process_registry.py`: running/finished process session registry, pending output drain, output caps, TTL cleanup.
- Modify `framework/tools/terminal/backends/base.py`: replace old backend ABC shape with PTY stream operations needed by both visible and hidden managers.
- Create `framework/tools/terminal/backends/windows_hidden.py`: hidden Windows PTY backend.
- Modify `framework/tools/terminal/backends/visible_windows.py`: adapt visible backend to the new backend protocol and add `current_segment`.
- Modify `framework/tools/terminal/backends/visible_windows_host.py`: expose screen/current-segment capture over the socket protocol.
- Create `framework/tools/terminal/managers.py`: `TerminalManager` protocol plus `WindowsHiddenTerminalManager` and `WindowsVisibleTerminalManager`.
- Create `framework/tools/terminal/command_tool.py`: new `command` tool.
- Create `framework/tools/terminal/process_tool.py`: new `process` tool.
- Modify `framework/tools/terminal/tool.py`: keep `TerminalTool`, add `current`, remove execution semantics.
- Modify `framework/tools/terminal/__init__.py`: export new tools/managers/types.
- Modify `framework/tools/standard/__init__.py`: remove `ShellTool` exports, export file/search tools only.
- Delete or stop registering `framework/tools/standard/shell_tool.py`; keep the file until the final cleanup task if imports still exist during migration.
- Modify `framework/ioc/configs/pool.py`: add terminal manager/default timeout fields.
- Modify `framework/ioc/configs/agent.py`: remove or deprecate `use_terminal` once pool terminal config controls manager creation.
- Modify `examples/bot_project/bot/service/pool_builder.py`: create configured manager, register `CommandTool`, `ProcessTool`, `TerminalTool`.
- Modify `examples/bot_project/bot/service/builders.py`: remove legacy `_make_shell_tool`.
- Modify `examples/bot_project/bot/service/core.py`: update pipeline-mode terminal construction if retained.
- Modify `examples/bot_project/config/pools/main.yml` and `coding.yml`: replace old terminal fields with manager/timeout defaults and update approval tool name from `shell` to `command`.
- Create/update tests under `tests/framework/tools/terminal/`.
- Update `examples/bot_project/tests/test_terminal_integration.py`.

## Invariants

- No subprocess fallback remains for command execution.
- Tools never expose `visible` or `hidden` as command parameters.
- Tool behavior is identical for visible and hidden managers.
- `command.timeout` must be lower than the ToolManager/runtime outer timeout.
- `yield_ms` returns a running session; it does not terminate the command.
- `timeout` terminates the command and returns `status="timed_out"` with captured output.
- `waiting_for_input` is a hint, not proof.
- Approval remains on the existing `ToolNode -> ApprovalTransaction -> TurnSnapshot -> ApprovalRenderer` path.

---

### Task 1: Terminal Config and Result Types

**Files:**
- Create: `framework/tools/terminal/config.py`
- Create: `framework/tools/terminal/results.py`
- Modify: `framework/tools/terminal/types.py`
- Test: `tests/framework/tools/terminal/test_config_and_results.py`

- [ ] **Step 1: Write failing config/result tests**

Create `tests/framework/tools/terminal/test_config_and_results.py`:

```python
from __future__ import annotations

from framework.tools.terminal.config import (
    TerminalRuntimeConfig,
    clamp_int,
    resolve_command_timeout,
)
from framework.tools.terminal.results import CommandResult, TerminalRead, TerminalSegment
from framework.tools.terminal.types import Platform, ProcessStatus, ShellFamily, TerminalVisibility


def test_terminal_runtime_config_defaults_keep_inner_timeout_below_outer() -> None:
    cfg = TerminalRuntimeConfig()

    assert cfg.default_yield_ms == 10_000
    assert cfg.default_command_timeout_seconds == 60
    assert cfg.command_tool_outer_timeout_seconds == 70
    assert cfg.default_command_timeout_seconds < cfg.command_tool_outer_timeout_seconds


def test_resolve_command_timeout_clamps_below_outer_timeout() -> None:
    cfg = TerminalRuntimeConfig(command_tool_outer_timeout_seconds=30)

    assert resolve_command_timeout(999, cfg) == 25
    assert resolve_command_timeout(-5, cfg) == 1


def test_clamp_int_accepts_none_and_bounds_values() -> None:
    assert clamp_int(None, default=10, minimum=1, maximum=20) == 10
    assert clamp_int(0, default=10, minimum=1, maximum=20) == 1
    assert clamp_int(25, default=10, minimum=1, maximum=20) == 20


def test_result_dataclasses_are_structured() -> None:
    result = CommandResult(
        status=ProcessStatus.RUNNING,
        session_id="ps-1",
        terminal="default",
        output="hello",
        tail="hello",
        timed_out=False,
    )
    read = TerminalRead(stdout="out", stderr="", raw="out")
    segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    assert result.status is ProcessStatus.RUNNING
    assert read.stdout == "out"
    assert segment.is_empty_prompt is True


def test_new_enums_cover_windows_first_design() -> None:
    assert Platform.WINDOWS.value == "windows"
    assert ShellFamily.POWERSHELL.value == "powershell"
    assert TerminalVisibility.HIDDEN.value == "hidden"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/test_config_and_results.py -v
```

Expected: import failures for `TerminalRuntimeConfig`, `CommandResult`, and new enums.

- [ ] **Step 3: Implement config and results**

Create `framework/tools/terminal/config.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


def clamp_int(value: int | None, *, default: int, minimum: int, maximum: int) -> int:
    candidate = default if value is None else int(value)
    return max(minimum, min(maximum, candidate))


@dataclass(frozen=True)
class TerminalRuntimeConfig:
    default_yield_ms: int = 10_000
    min_yield_ms: int = 10
    max_yield_ms: int = 120_000
    default_command_timeout_seconds: int = 60
    command_tool_outer_timeout_seconds: int = 70
    input_wait_idle_ms: int = 15_000
    min_input_wait_idle_ms: int = 1_000
    max_input_wait_idle_ms: int = 600_000
    poll_max_wait_ms: int = 30_000
    max_output_chars: int = 200_000
    pending_max_output_chars: int = 30_000
    finished_ttl_ms: int = 1_800_000


def resolve_yield_ms(value: int | None, config: TerminalRuntimeConfig) -> int:
    return clamp_int(
        value,
        default=config.default_yield_ms,
        minimum=config.min_yield_ms,
        maximum=config.max_yield_ms,
    )


def resolve_command_timeout(value: int | None, config: TerminalRuntimeConfig) -> int:
    max_inner_timeout = max(1, config.command_tool_outer_timeout_seconds - 5)
    return clamp_int(
        value,
        default=config.default_command_timeout_seconds,
        minimum=1,
        maximum=max_inner_timeout,
    )


def resolve_poll_wait_ms(value: int | None, config: TerminalRuntimeConfig) -> int:
    return clamp_int(value, default=0, minimum=0, maximum=config.poll_max_wait_ms)
```

Create `framework/tools/terminal/results.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from framework.tools.terminal.types import ProcessStatus


@dataclass(frozen=True)
class TerminalRead:
    stdout: str = ""
    stderr: str = ""
    raw: str = ""


@dataclass(frozen=True)
class TerminalSegment:
    text: str
    cursor_line: str = ""
    is_empty_prompt: bool = False


@dataclass(frozen=True)
class CommandResult:
    status: ProcessStatus
    session_id: str | None
    terminal: str
    output: str
    tail: str
    pid: int | None = None
    cwd: str | None = None
    exit_code: int | None = None
    exit_signal: str | int | None = None
    timed_out: bool = False
    duration_ms: int | None = None
    failure_kind: str | None = None
    message: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    truncated: bool = False
    stdin_writable: bool | None = None
    waiting_for_input: bool | None = None
    idle_ms: int | None = None


@dataclass(frozen=True)
class ProcessActionResult:
    status: ProcessStatus
    session_id: str | None
    text: str
    details: dict[str, object]
```

Modify `framework/tools/terminal/types.py`:

```python
class ShellFamily(StrEnum):
    BASH = "bash"
    CMD = "cmd"
    POWERSHELL = "powershell"
    ZSH = "zsh"
    SH = "sh"


class TerminalVisibility(StrEnum):
    VISIBLE = "visible"
    HIDDEN = "hidden"


class ProcessStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"
    TIMED_OUT = "timed_out"
```

Keep existing `Platform`, `ShellInfo`, and shell detection functions. Add `POWERSHELL` mapping in `_family_from_path`.

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/test_config_and_results.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add framework/tools/terminal/config.py framework/tools/terminal/results.py framework/tools/terminal/types.py tests/framework/tools/terminal/test_config_and_results.py
git commit -m "feat(terminal): add command runtime types"
```

---

### Task 2: Process Registry

**Files:**
- Create: `framework/tools/terminal/process_registry.py`
- Test: `tests/framework/tools/terminal/test_process_registry.py`

- [ ] **Step 1: Write failing process registry tests**

Create `tests/framework/tools/terminal/test_process_registry.py`:

```python
from __future__ import annotations

import time

from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.types import ProcessStatus


def test_append_output_tracks_pending_aggregated_and_tail() -> None:
    registry = ProcessRegistry(config=TerminalRuntimeConfig(max_output_chars=20, pending_max_output_chars=10))
    session = registry.create(command="echo hello", terminal="default", cwd="C:\\repo", pid=123)

    registry.append_output(session.id, "stdout", "hello ")
    registry.append_output(session.id, "stdout", "world")

    drained = registry.drain_pending(session.id)
    current = registry.get_running(session.id)

    assert drained.stdout == "llo world"
    assert drained.stderr == ""
    assert current is not None
    assert current.aggregated == "hello world"
    assert current.tail == "hello world"
    assert current.truncated is True


def test_drain_pending_output_is_not_repeated() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="npm run dev", terminal="web", cwd=None, pid=222)
    registry.append_output(session.id, "stdout", "ready\n")

    first = registry.drain_pending(session.id)
    second = registry.drain_pending(session.id)

    assert first.stdout == "ready\n"
    assert second.stdout == ""


def test_waiting_for_input_is_idle_and_stdin_writable_hint() -> None:
    registry = ProcessRegistry(config=TerminalRuntimeConfig(input_wait_idle_ms=1000))
    session = registry.create(command="ssh host", terminal="remote", cwd=None, pid=333)
    session.stdin_writable = True
    session.last_output_at = time.time() - 2

    runtime = registry.running_runtime(session.id)

    assert runtime is not None
    assert runtime.waiting_for_input is True
    assert runtime.idle_ms >= 1000


def test_mark_exited_moves_session_to_finished() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="python script.py", terminal="default", cwd=None, pid=444)

    registry.mark_exited(session.id, exit_code=0, exit_signal=None, status=ProcessStatus.COMPLETED)

    assert registry.get_running(session.id) is None
    assert registry.get_finished(session.id) is not None
    assert registry.get_finished(session.id).status is ProcessStatus.COMPLETED


def test_prune_finished_sessions_removes_expired_records() -> None:
    registry = ProcessRegistry(config=TerminalRuntimeConfig(finished_ttl_ms=10))
    session = registry.create(command="echo done", terminal="default", cwd=None, pid=555)
    registry.mark_exited(session.id, exit_code=0, exit_signal=None, status=ProcessStatus.COMPLETED)

    finished = registry.get_finished(session.id)
    assert finished is not None
    finished.ended_at = time.time() - 1
    registry.prune_finished()

    assert registry.get_finished(session.id) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/test_process_registry.py -v
```

Expected: import failure for `ProcessRegistry`.

- [ ] **Step 3: Implement process registry**

Create `framework/tools/terminal/process_registry.py`:

```python
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Literal

from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.results import TerminalRead
from framework.tools.terminal.types import ProcessStatus

StreamName = Literal["stdout", "stderr"]


@dataclass
class RunningSessionRuntime:
    stdin_writable: bool
    waiting_for_input: bool
    idle_ms: int
    last_output_at: float


@dataclass
class ProcessSession:
    id: str
    terminal: str
    command: str
    pid: int | None
    cwd: str | None
    started_at: float
    status: ProcessStatus = ProcessStatus.RUNNING
    stdin_writable: bool = True
    last_output_at: float = field(default_factory=time.time)
    pending_stdout: list[str] = field(default_factory=list)
    pending_stderr: list[str] = field(default_factory=list)
    aggregated: str = ""
    tail: str = ""
    total_output_chars: int = 0
    max_output_chars: int = 200_000
    pending_max_output_chars: int = 30_000
    truncated: bool = False
    ended_at: float | None = None
    exit_code: int | None = None
    exit_signal: str | int | None = None
    timed_out: bool = False
    failure_kind: str | None = None


class ProcessRegistry:
    def __init__(self, config: TerminalRuntimeConfig | None = None) -> None:
        self._config = config or TerminalRuntimeConfig()
        self._running: dict[str, ProcessSession] = {}
        self._finished: dict[str, ProcessSession] = {}

    def create(self, *, command: str, terminal: str, cwd: str | None, pid: int | None) -> ProcessSession:
        session_id = self._new_id()
        session = ProcessSession(
            id=session_id,
            terminal=terminal,
            command=command,
            pid=pid,
            cwd=cwd,
            started_at=time.time(),
            max_output_chars=self._config.max_output_chars,
            pending_max_output_chars=self._config.pending_max_output_chars,
        )
        self._running[session_id] = session
        return session

    def get_running(self, session_id: str) -> ProcessSession | None:
        return self._running.get(session_id)

    def get_finished(self, session_id: str) -> ProcessSession | None:
        return self._finished.get(session_id)

    def list_running(self) -> list[ProcessSession]:
        return sorted(self._running.values(), key=lambda item: item.started_at, reverse=True)

    def list_finished(self) -> list[ProcessSession]:
        return sorted(self._finished.values(), key=lambda item: item.started_at, reverse=True)

    def delete(self, session_id: str) -> bool:
        existed = session_id in self._running or session_id in self._finished
        self._running.pop(session_id, None)
        self._finished.pop(session_id, None)
        return existed

    def append_output(self, session_id: str, stream: StreamName, chunk: str) -> None:
        session = self._running[session_id]
        session.last_output_at = time.time()
        session.total_output_chars += len(chunk)
        pending = session.pending_stdout if stream == "stdout" else session.pending_stderr
        pending.append(chunk)
        self._cap_pending(pending, session)
        combined = session.aggregated + chunk
        if len(combined) > session.max_output_chars:
            session.truncated = True
            combined = combined[-session.max_output_chars :]
        session.aggregated = combined
        session.tail = combined[-2000:]

    def drain_pending(self, session_id: str) -> TerminalRead:
        session = self._running.get(session_id) or self._finished.get(session_id)
        if session is None:
            return TerminalRead()
        stdout = "".join(session.pending_stdout)
        stderr = "".join(session.pending_stderr)
        session.pending_stdout.clear()
        session.pending_stderr.clear()
        return TerminalRead(stdout=stdout, stderr=stderr, raw=stdout + stderr)

    def mark_exited(
        self,
        session_id: str,
        *,
        exit_code: int | None,
        exit_signal: str | int | None,
        status: ProcessStatus,
        timed_out: bool = False,
        failure_kind: str | None = None,
    ) -> ProcessSession | None:
        session = self._running.pop(session_id, None)
        if session is None:
            return None
        session.status = status
        session.exit_code = exit_code
        session.exit_signal = exit_signal
        session.ended_at = time.time()
        session.timed_out = timed_out
        session.failure_kind = failure_kind
        self._finished[session_id] = session
        return session

    def running_runtime(self, session_id: str) -> RunningSessionRuntime | None:
        session = self._running.get(session_id)
        if session is None:
            return None
        idle_ms = max(0, int((time.time() - session.last_output_at) * 1000))
        return RunningSessionRuntime(
            stdin_writable=session.stdin_writable,
            waiting_for_input=session.stdin_writable and idle_ms >= self._config.input_wait_idle_ms,
            idle_ms=idle_ms,
            last_output_at=session.last_output_at,
        )

    def prune_finished(self) -> None:
        cutoff = time.time() - (self._config.finished_ttl_ms / 1000)
        expired = [
            session_id
            for session_id, session in self._finished.items()
            if session.ended_at is not None and session.ended_at < cutoff
        ]
        for session_id in expired:
            self._finished.pop(session_id, None)

    def _new_id(self) -> str:
        while True:
            session_id = f"ps-{secrets.token_hex(4)}"
            if session_id not in self._running and session_id not in self._finished:
                return session_id

    def _cap_pending(self, pending: list[str], session: ProcessSession) -> None:
        total = sum(len(item) for item in pending)
        if total <= session.pending_max_output_chars:
            return
        session.truncated = True
        text = "".join(pending)[-session.pending_max_output_chars :]
        pending.clear()
        pending.append(text)
```

- [ ] **Step 4: Run tests**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/test_process_registry.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add framework/tools/terminal/process_registry.py tests/framework/tools/terminal/test_process_registry.py
git commit -m "feat(terminal): add process registry"
```

---

### Task 3: Backend Protocol and Fake Backend Contract

**Files:**
- Modify: `framework/tools/terminal/backends/base.py`
- Test: `tests/framework/tools/terminal/test_backend_contract.py`

- [ ] **Step 1: Write failing backend contract tests**

Create `tests/framework/tools/terminal/test_backend_contract.py`:

```python
from __future__ import annotations

import pytest

from framework.tools.terminal.backends.base import TerminalBackend
from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.types import Platform, TerminalVisibility


class FakeBackend(TerminalBackend):
    platform = Platform.WINDOWS
    visibility = TerminalVisibility.HIDDEN

    def __init__(self) -> None:
        self.started = False
        self.writes: list[str] = []

    async def start(self, shell, cwd, env) -> None:
        self.started = True

    async def write(self, data: str) -> None:
        self.writes.append(data)

    async def read_pending(self, timeout: float, max_size: int) -> TerminalRead:
        return TerminalRead(stdout="out", raw="out")

    async def current_segment(self) -> TerminalSegment:
        return TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    async def interrupt(self) -> None:
        self.writes.append("\x03")

    async def terminate(self) -> None:
        self.started = False

    async def kill(self) -> None:
        self.started = False

    async def is_alive(self) -> bool:
        return self.started

    def stdin_writable(self) -> bool:
        return self.started


@pytest.mark.asyncio
async def test_backend_protocol_supports_stream_operations() -> None:
    backend = FakeBackend()

    await backend.start(shell=None, cwd=None, env=None)
    await backend.write("echo hi\r")
    read = await backend.read_pending(timeout=0.1, max_size=100)
    segment = await backend.current_segment()
    await backend.interrupt()

    assert await backend.is_alive() is True
    assert backend.stdin_writable() is True
    assert read.stdout == "out"
    assert segment.is_empty_prompt is True
    assert backend.writes == ["echo hi\r", "\x03"]
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/test_backend_contract.py -v
```

Expected: base backend does not expose the new protocol.

- [ ] **Step 3: Update backend base protocol**

Modify `framework/tools/terminal/backends/base.py` so `TerminalBackend` defines the methods used in the test. Use `abc.ABC` with abstract async methods if the current file already uses ABCs; keep method names exactly as in the test.

The protocol must include:

```python
async def read_pending(self, timeout: float, max_size: int) -> TerminalRead
async def current_segment(self) -> TerminalSegment
async def interrupt(self) -> None
def stdin_writable(self) -> bool
```

Keep compatibility wrappers only if existing tests still need `read`; the wrappers should call `read_pending` and return `.raw`.

- [ ] **Step 4: Run backend tests**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/test_backend_contract.py tests/framework/tools/terminal/backends/test_base.py -v
```

Expected: new contract tests pass; update old base tests to the new method names if they fail due intentional API replacement.

- [ ] **Step 5: Commit**

```powershell
git add framework/tools/terminal/backends/base.py tests/framework/tools/terminal/test_backend_contract.py tests/framework/tools/terminal/backends/test_base.py
git commit -m "refactor(terminal): define backend stream contract"
```

---

### Task 4: Terminal Managers and Session Orchestration With Fake Backends

**Files:**
- Create: `framework/tools/terminal/managers.py`
- Modify: `framework/tools/terminal/session.py`
- Test: `tests/framework/tools/terminal/test_managers.py`

- [ ] **Step 1: Write failing manager tests with fake backends**

Create `tests/framework/tools/terminal/test_managers.py`:

```python
from __future__ import annotations

import pytest

from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.managers import BaseTerminalManager
from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.types import Platform, ShellFamily, ShellInfo, TerminalVisibility


class FakeBackend:
    platform = Platform.WINDOWS
    visibility = TerminalVisibility.HIDDEN

    def __init__(self) -> None:
        self.started = False
        self.writes: list[str] = []

    async def start(self, shell, cwd, env) -> None:
        self.started = True

    async def write(self, data: str) -> None:
        self.writes.append(data)

    async def read_pending(self, timeout: float, max_size: int) -> TerminalRead:
        return TerminalRead(stdout="ready\n", raw="ready\n")

    async def current_segment(self) -> TerminalSegment:
        return TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    async def interrupt(self) -> None:
        self.writes.append("\x03")

    async def terminate(self) -> None:
        self.started = False

    async def kill(self) -> None:
        self.started = False

    async def is_alive(self) -> bool:
        return self.started

    def stdin_writable(self) -> bool:
        return self.started


@pytest.mark.asyncio
async def test_manager_creates_default_session_without_tool_knowing_visibility() -> None:
    manager = BaseTerminalManager(
        shell_info=ShellInfo(ShellFamily.CMD, "cmd.exe", Platform.WINDOWS),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=FakeBackend,
        config=TerminalRuntimeConfig(),
    )

    session = await manager.get_or_create(None)
    default = await manager.get_default()

    assert session.name == "default"
    assert default is session
    assert manager.visibility is TerminalVisibility.HIDDEN


@pytest.mark.asyncio
async def test_terminal_session_start_write_poll_and_current_segment() -> None:
    manager = BaseTerminalManager(
        shell_info=ShellInfo(ShellFamily.CMD, "cmd.exe", Platform.WINDOWS),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=FakeBackend,
        config=TerminalRuntimeConfig(),
    )
    session = await manager.get_or_create("build")

    await session.ensure_started()
    await session.write("npm test\r")
    read = await session.poll_once()
    segment = await session.current_segment()

    assert read.stdout == "ready\n"
    assert segment.text == "$ "


@pytest.mark.asyncio
async def test_manager_select_and_close() -> None:
    manager = BaseTerminalManager(
        shell_info=ShellInfo(ShellFamily.CMD, "cmd.exe", Platform.WINDOWS),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=FakeBackend,
        config=TerminalRuntimeConfig(),
    )
    await manager.get_or_create("one")
    await manager.get_or_create("two")

    await manager.select_default("two")
    closed = await manager.close("two")
    default = await manager.get_default()

    assert closed is True
    assert default.name == "one"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/test_managers.py -v
```

Expected: import failure for `BaseTerminalManager`.

- [ ] **Step 3: Implement `BaseTerminalManager` and session primitives**

Modify `framework/tools/terminal/session.py` to make `TerminalSession` expose:

```python
async def ensure_started(self) -> None
async def write(self, data: str) -> None
async def poll_once(self, timeout: float = 0.1, max_size: int = 65536) -> TerminalRead
async def current_segment(self) -> TerminalSegment
async def interrupt(self) -> None
async def terminate(self) -> None
```

Create `framework/tools/terminal/managers.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.session import TerminalInfo, TerminalSession
from framework.tools.terminal.types import Platform, ShellInfo, TerminalVisibility


class TerminalManagerProtocol(Protocol):
    platform: Platform
    shell_info: ShellInfo
    visibility: TerminalVisibility

    async def get_or_create(self, name: str | None, workdir: str | None = None) -> TerminalSession: ...
    async def get_default(self) -> TerminalSession: ...
    async def select_default(self, name: str) -> None: ...
    async def list_sessions(self) -> list[TerminalInfo]: ...
    async def close(self, name: str) -> bool: ...


class BaseTerminalManager:
    def __init__(
        self,
        *,
        shell_info: ShellInfo,
        visibility: TerminalVisibility,
        backend_factory: Callable[[], object],
        config: TerminalRuntimeConfig | None = None,
    ) -> None:
        self.platform = shell_info.platform
        self.shell_info = shell_info
        self.visibility = visibility
        self.config = config or TerminalRuntimeConfig()
        self._backend_factory = backend_factory
        self._sessions: dict[str, TerminalSession] = {}
        self._default_name: str | None = None

    async def get_or_create(self, name: str | None, workdir: str | None = None) -> TerminalSession:
        session_name = name or "default"
        session = self._sessions.get(session_name)
        if session is not None:
            return session
        backend = self._backend_factory()
        session = TerminalSession(
            name=session_name,
            backend=backend,
            shell_info=self.shell_info,
            cwd=workdir,
        )
        self._sessions[session_name] = session
        if self._default_name is None:
            self._default_name = session_name
        return session

    async def get_default(self) -> TerminalSession:
        if self._default_name is None:
            return await self.get_or_create("default")
        return self._sessions[self._default_name]

    async def select_default(self, name: str) -> None:
        if name not in self._sessions:
            raise ValueError(f"Terminal '{name}' does not exist")
        self._default_name = name

    async def list_sessions(self) -> list[TerminalInfo]:
        result: list[TerminalInfo] = []
        for name, session in self._sessions.items():
            result.append(await session.to_info(is_default=name == self._default_name))
        return result

    async def close(self, name: str) -> bool:
        session = self._sessions.pop(name, None)
        if session is None:
            return False
        await session.terminate()
        if self._default_name == name:
            self._default_name = next(iter(self._sessions), None)
        return True
```

- [ ] **Step 4: Run manager tests**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/test_managers.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add framework/tools/terminal/managers.py framework/tools/terminal/session.py tests/framework/tools/terminal/test_managers.py
git commit -m "feat(terminal): add manager abstraction"
```

---

### Task 5: Command Tool With Fake Manager

**Files:**
- Create: `framework/tools/terminal/command_tool.py`
- Test: `tests/framework/tools/terminal/test_command_tool.py`

- [ ] **Step 1: Write failing command tool tests**

Create `tests/framework/tools/terminal/test_command_tool.py`:

```python
from __future__ import annotations

import asyncio

import pytest

from framework.tools.terminal.command_tool import CommandTool
from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.types import Platform, ProcessStatus, ShellFamily, ShellInfo, TerminalVisibility
from framework.tools.terminal.managers import BaseTerminalManager


class FakeBackend:
    platform = Platform.WINDOWS
    visibility = TerminalVisibility.HIDDEN

    def __init__(self) -> None:
        self.started = False
        self.writes: list[str] = []
        self.reads: list[TerminalRead] = []
        self.alive = True

    async def start(self, shell, cwd, env) -> None:
        self.started = True

    async def write(self, data: str) -> None:
        self.writes.append(data)

    async def read_pending(self, timeout: float, max_size: int) -> TerminalRead:
        if self.reads:
            return self.reads.pop(0)
        await asyncio.sleep(0)
        return TerminalRead()

    async def current_segment(self) -> TerminalSegment:
        return TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    async def interrupt(self) -> None:
        self.writes.append("\x03")

    async def terminate(self) -> None:
        self.alive = False

    async def kill(self) -> None:
        self.alive = False

    async def is_alive(self) -> bool:
        return self.alive

    def stdin_writable(self) -> bool:
        return self.alive


def make_tool(config: TerminalRuntimeConfig | None = None) -> tuple[CommandTool, BaseTerminalManager, ProcessRegistry]:
    manager = BaseTerminalManager(
        shell_info=ShellInfo(ShellFamily.CMD, "cmd.exe", Platform.WINDOWS),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=FakeBackend,
        config=config or TerminalRuntimeConfig(),
    )
    registry = ProcessRegistry(config=config or TerminalRuntimeConfig())
    return CommandTool(manager=manager, registry=registry, config=config or TerminalRuntimeConfig()), manager, registry


@pytest.mark.asyncio
async def test_command_returns_completed_result_before_yield_window() -> None:
    tool, manager, _registry = make_tool()
    session = await manager.get_or_create("default")
    session._backend.reads = [TerminalRead(stdout="done\n", raw="done\n")]
    session._backend.alive = False

    result = await tool.execute(command="echo done", terminal="default", yield_ms=1000)

    assert "done" in result


@pytest.mark.asyncio
async def test_command_background_returns_running_session_id() -> None:
    tool, _manager, registry = make_tool()

    result = await tool.execute(command="npm run dev", terminal="web", background=True)

    running = registry.list_running()
    assert "status=running" in result
    assert len(running) == 1
    assert running[0].command == "npm run dev"


@pytest.mark.asyncio
async def test_command_timeout_returns_timed_out_with_captured_output() -> None:
    cfg = TerminalRuntimeConfig(default_command_timeout_seconds=1, command_tool_outer_timeout_seconds=3)
    tool, manager, _registry = make_tool(cfg)
    session = await manager.get_or_create("default")
    session._backend.reads = [TerminalRead(stdout="partial\n", raw="partial\n")]

    result = await tool.execute(command="slow", terminal="default", timeout=1, yield_ms=120000)

    assert "status=timed_out" in result
    assert "partial" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/test_command_tool.py -v
```

Expected: import failure for `CommandTool`.

- [ ] **Step 3: Implement `CommandTool` minimal behavior**

Create `framework/tools/terminal/command_tool.py`. Implement it as a `Tool` with `name == "command"`. It should:

- resolve terminal via manager;
- create a `ProcessSession`;
- start the terminal;
- write `command + "\r"` for Windows first phase;
- loop until command exits, `yield_ms` elapses, or `timeout` elapses;
- append output to registry;
- return text with explicit `status=...` lines while also keeping structured data in local helper methods for later formatting.

Implementation skeleton:

```python
from __future__ import annotations

import time
from typing import Any

from framework.core.tool_manager import Tool
from framework.tools.terminal.config import TerminalRuntimeConfig, resolve_command_timeout, resolve_yield_ms
from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.types import ProcessStatus


class CommandTool(Tool):
    def __init__(self, manager: Any, registry: ProcessRegistry, config: TerminalRuntimeConfig | None = None) -> None:
        super().__init__()
        self._manager = manager
        self._registry = registry
        self._config = config or TerminalRuntimeConfig()
        self.config.timeout = self._config.command_tool_outer_timeout_seconds

    @property
    def name(self) -> str:
        return "command"

    @property
    def description(self) -> str:
        return (
            "Start a command in a named terminal session. If the command is still running "
            "after the yield window, returns a session id for the process tool. "
            "Use process poll/log/write/submit/send_keys/paste/interrupt/kill for follow-up."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "terminal": {"type": "string"},
                "workdir": {"type": "string"},
                "env": {"type": "object", "additionalProperties": {"type": "string"}},
                "timeout": {"type": "number"},
                "yield_ms": {"type": "number"},
                "background": {"type": "boolean"},
                "pty": {"type": "boolean"},
            },
            "required": ["command"],
        }

    async def execute(
        self,
        command: str,
        terminal: str | None = None,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
        yield_ms: int | None = None,
        background: bool = False,
        pty: bool = True,
        **_kwargs: object,
    ) -> str:
        session = await self._manager.get_or_create(terminal, workdir=workdir)
        await session.ensure_started()
        proc = self._registry.create(command=command, terminal=session.name, cwd=workdir, pid=None)
        await session.write(command + "\r")
        inner_timeout = resolve_command_timeout(timeout, self._config)
        yield_window_ms = 0 if background else resolve_yield_ms(yield_ms, self._config)
        start = time.monotonic()
        output_parts: list[str] = []

        while True:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if elapsed_ms >= inner_timeout * 1000:
                read = await session.poll_once(timeout=0.05)
                if read.stdout:
                    self._registry.append_output(proc.id, "stdout", read.stdout)
                    output_parts.append(read.stdout)
                await session.terminate()
                self._registry.mark_exited(
                    proc.id,
                    exit_code=None,
                    exit_signal="TIMEOUT",
                    status=ProcessStatus.TIMED_OUT,
                    timed_out=True,
                    failure_kind="overall-timeout",
                )
                return self._format_result(
                    status=ProcessStatus.TIMED_OUT,
                    session_id=proc.id,
                    output="".join(output_parts),
                    message=f"Timed out after {inner_timeout}s; process terminated.",
                )

            read = await session.poll_once(timeout=0.05)
            if read.stdout:
                self._registry.append_output(proc.id, "stdout", read.stdout)
                output_parts.append(read.stdout)
            if read.stderr:
                self._registry.append_output(proc.id, "stderr", read.stderr)
                output_parts.append(read.stderr)

            alive = await session.is_alive()
            if not alive:
                self._registry.mark_exited(proc.id, exit_code=0, exit_signal=None, status=ProcessStatus.COMPLETED)
                return self._format_result(
                    status=ProcessStatus.COMPLETED,
                    session_id=proc.id,
                    output="".join(output_parts),
                    message=None,
                )

            runtime = self._registry.running_runtime(proc.id)
            if runtime is not None and runtime.waiting_for_input:
                return self._format_result(
                    status=ProcessStatus.RUNNING,
                    session_id=proc.id,
                    output="".join(output_parts),
                    message=f"No new output for {runtime.idle_ms}ms; session may be waiting for input.",
                )

            if elapsed_ms >= yield_window_ms:
                return self._format_result(
                    status=ProcessStatus.RUNNING,
                    session_id=proc.id,
                    output="".join(output_parts),
                    message="Command still running. Use process poll/log/write/submit/send_keys/paste/interrupt/kill.",
                )

    def _format_result(
        self,
        *,
        status: ProcessStatus,
        session_id: str,
        output: str,
        message: str | None,
    ) -> str:
        lines = [
            f"status={status.value}",
            f"session_id={session_id}",
        ]
        if output:
            lines.append("output:")
            lines.append(output.rstrip())
        if message:
            lines.append(message)
        return "\n".join(lines)
```

The fake-backend tests intentionally exercise only the orchestration contract. Windows process exit detection will become stronger in the backend tasks.

- [ ] **Step 4: Run command tool tests**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/test_command_tool.py -v
```

Expected: all tests pass after adapting any references to private fake backend attributes.

- [ ] **Step 5: Commit**

```powershell
git add framework/tools/terminal/command_tool.py tests/framework/tools/terminal/test_command_tool.py
git commit -m "feat(terminal): add command tool"
```

---

### Task 6: Process Tool Follow-Up Actions

**Files:**
- Create: `framework/tools/terminal/process_tool.py`
- Test: `tests/framework/tools/terminal/test_process_tool.py`

- [ ] **Step 1: Write failing process tool tests**

Create `tests/framework/tools/terminal/test_process_tool.py`:

```python
from __future__ import annotations

import pytest

from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.process_tool import ProcessTool
from framework.tools.terminal.types import ProcessStatus


class FakeTerminal:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.interrupted = False
        self.killed = False

    async def write(self, data: str) -> None:
        self.writes.append(data)

    async def interrupt(self) -> None:
        self.interrupted = True

    async def terminate(self) -> None:
        self.killed = True


class FakeManager:
    def __init__(self, terminal: FakeTerminal) -> None:
        self.terminal = terminal

    async def get_or_create(self, name, workdir=None):
        return self.terminal


@pytest.mark.asyncio
async def test_process_poll_drains_pending_once() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="server", terminal="web", cwd=None, pid=1)
    registry.append_output(session.id, "stdout", "ready\n")
    tool = ProcessTool(registry=registry, manager=FakeManager(FakeTerminal()))

    first = await tool.execute(action="poll", session_id=session.id)
    second = await tool.execute(action="poll", session_id=session.id)

    assert "ready" in first
    assert "(no new output)" in second


@pytest.mark.asyncio
async def test_process_write_submit_interrupt_and_kill() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="ssh host", terminal="remote", cwd=None, pid=2)
    terminal = FakeTerminal()
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    await tool.execute(action="write", session_id=session.id, data="password")
    await tool.execute(action="submit", session_id=session.id)
    await tool.execute(action="interrupt", session_id=session.id)
    await tool.execute(action="kill", session_id=session.id)

    assert terminal.writes == ["password", "\r"]
    assert terminal.interrupted is True
    assert terminal.killed is True
    assert registry.get_finished(session.id).status is ProcessStatus.KILLED


@pytest.mark.asyncio
async def test_process_log_reads_aggregated_output() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="build", terminal="default", cwd=None, pid=3)
    registry.append_output(session.id, "stdout", "line1\nline2\n")
    tool = ProcessTool(registry=registry, manager=FakeManager(FakeTerminal()))

    text = await tool.execute(action="log", session_id=session.id, offset=1, limit=1)

    assert "line2" in text
    assert "line1" not in text
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/test_process_tool.py -v
```

Expected: import failure for `ProcessTool`.

- [ ] **Step 3: Implement `ProcessTool`**

Create `framework/tools/terminal/process_tool.py`. Implement these actions first: `list`, `poll`, `log`, `write`, `submit`, `interrupt`, `kill`, `clear`, `remove`. Add `send_keys` and `paste` with simple literal/hex support in Task 9.

Required behavior:

- `poll`: calls `registry.drain_pending(session_id)`.
- `log`: reads `session.aggregated`, splits lines, applies `offset` and `limit`.
- `write`: writes `data` exactly as given.
- `submit`: writes `"\r"` for Windows first phase.
- `interrupt`: calls terminal interrupt.
- `kill`: terminates terminal/session and marks registry status `KILLED`.
- `remove`: same as `kill` for running sessions, delete for finished sessions.

- [ ] **Step 4: Run process tool tests**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/test_process_tool.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add framework/tools/terminal/process_tool.py tests/framework/tools/terminal/test_process_tool.py
git commit -m "feat(terminal): add process tool"
```

---

### Task 7: Terminal Tool `current` Action

**Files:**
- Modify: `framework/tools/terminal/tool.py`
- Test: `tests/framework/tools/terminal/test_terminal_tool_current.py`

- [ ] **Step 1: Write failing terminal current tests**

Create `tests/framework/tools/terminal/test_terminal_tool_current.py`:

```python
from __future__ import annotations

import pytest

from framework.tools.terminal.results import TerminalSegment
from framework.tools.terminal.tool import TerminalTool


class FakeSession:
    name = "default"

    async def current_segment(self) -> TerminalSegment:
        return TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    async def interrupt(self) -> None:
        pass


class FakeManager:
    async def get_default(self):
        return FakeSession()

    async def get_or_create(self, name=None, workdir=None):
        return FakeSession()

    async def list_sessions(self):
        return []

    def list_names(self):
        return ["default"]


@pytest.mark.asyncio
async def test_terminal_current_returns_empty_prompt_as_current_segment() -> None:
    tool = TerminalTool(FakeManager())

    result = await tool.execute(action="current")

    assert "Current terminal segment" in result
    assert "$ " in result
    assert "empty_prompt=True" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/test_terminal_tool_current.py -v
```

Expected: `current` action is unknown.

- [ ] **Step 3: Add `current` to `TerminalTool`**

Modify `framework/tools/terminal/tool.py`:

- Add `CURRENT = "current"` to `TerminalAction`.
- Add it to parameter enum and description.
- Implement:

```python
if action_enum == TerminalAction.CURRENT:
    session = self._manager.get(name) if name else await self._manager.get_default()
    if session is None:
        return "Error: No terminal is active."
    segment = await session.current_segment()
    return (
        "Current terminal segment:\n"
        f"{segment.text}\n"
        f"empty_prompt={segment.is_empty_prompt}"
    )
```

If the new manager protocol has no `get(name)` method, use `await self._manager.get_or_create(name)` for named sessions.

- [ ] **Step 4: Run terminal current tests**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/test_terminal_tool_current.py tests/framework/tools/terminal/test_terminal_tool.py -v
```

Expected: all tests pass after updating old enum expectations.

- [ ] **Step 5: Commit**

```powershell
git add framework/tools/terminal/tool.py tests/framework/tools/terminal/test_terminal_tool_current.py tests/framework/tools/terminal/test_terminal_tool.py
git commit -m "feat(terminal): add current segment action"
```

---

### Task 8: Windows Hidden Backend

**Files:**
- Create: `framework/tools/terminal/backends/windows_hidden.py`
- Modify: `framework/tools/terminal/backends/__init__.py`
- Test: `tests/framework/tools/terminal/backends/test_windows_hidden.py`

- [ ] **Step 1: Write hidden backend tests**

Create `tests/framework/tools/terminal/backends/test_windows_hidden.py`:

```python
from __future__ import annotations

import sys

import pytest

from framework.tools.terminal.backends.windows_hidden import WindowsHiddenPtyBackend
from framework.tools.terminal.types import Platform, TerminalVisibility


def test_hidden_backend_declares_windows_hidden() -> None:
    backend = WindowsHiddenPtyBackend()

    assert backend.platform is Platform.WINDOWS
    assert backend.visibility is TerminalVisibility.HIDDEN


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PTY backend")
@pytest.mark.asyncio
async def test_hidden_backend_runs_cmd_echo() -> None:
    backend = WindowsHiddenPtyBackend()

    await backend.start(shell=None, cwd=None, env=None)
    await backend.write("echo hello\r")
    chunks = []
    for _ in range(20):
        read = await backend.read_pending(timeout=0.2, max_size=65536)
        if read.raw:
            chunks.append(read.raw)
        if "hello" in "".join(chunks).lower():
            break
    await backend.terminate()

    assert "hello" in "".join(chunks).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/backends/test_windows_hidden.py -v
```

Expected: import failure for `WindowsHiddenPtyBackend`.

- [ ] **Step 3: Implement hidden backend**

Implement `WindowsHiddenPtyBackend` using the same pywinpty dependency already required by the visible backend. It must:

- start a shell in a PTY without opening a visible console window;
- read PTY output into `TerminalRead(raw=..., stdout=...)`;
- implement `write`, `interrupt`, `terminate`, `kill`, `is_alive`, `stdin_writable`;
- return `TerminalSegment` from an internal recent screen buffer in `current_segment`.

Keep the first implementation Windows-only. On non-Windows, construction may succeed but `start()` should raise `RuntimeError("WindowsHiddenPtyBackend is only supported on Windows")`.

- [ ] **Step 4: Run hidden backend tests**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/backends/test_windows_hidden.py -v
```

Expected: non-Windows skips integration; Windows passes both tests.

- [ ] **Step 5: Commit**

```powershell
git add framework/tools/terminal/backends/windows_hidden.py framework/tools/terminal/backends/__init__.py tests/framework/tools/terminal/backends/test_windows_hidden.py
git commit -m "feat(terminal): add windows hidden backend"
```

---

### Task 9: Visible Backend Current Segment and Input Keys

**Files:**
- Modify: `framework/tools/terminal/backends/visible_windows.py`
- Modify: `framework/tools/terminal/backends/visible_windows_host.py`
- Modify: `framework/tools/terminal/process_tool.py`
- Test: `tests/framework/tools/terminal/backends/test_visible_windows_current.py`
- Test: `tests/framework/tools/terminal/test_process_tool_keys.py`

- [ ] **Step 1: Write visible current and key tests**

Create `tests/framework/tools/terminal/test_process_tool_keys.py`:

```python
from __future__ import annotations

import pytest

from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.process_tool import ProcessTool


class FakeTerminal:
    def __init__(self) -> None:
        self.writes: list[str] = []

    async def write(self, data: str) -> None:
        self.writes.append(data)


class FakeManager:
    def __init__(self, terminal: FakeTerminal) -> None:
        self.terminal = terminal

    async def get_or_create(self, name, workdir=None):
        return self.terminal


@pytest.mark.asyncio
async def test_send_keys_ctrl_c_escape_and_enter() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="less file", terminal="default", cwd=None, pid=1)
    terminal = FakeTerminal()
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    await tool.execute(action="send_keys", session_id=session.id, keys=["escape", "enter", "ctrl+c"])

    assert terminal.writes == ["\x1b\r\x03"]


@pytest.mark.asyncio
async def test_paste_writes_text_without_guessing_shell() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="python", terminal="default", cwd=None, pid=2)
    terminal = FakeTerminal()
    tool = ProcessTool(registry=registry, manager=FakeManager(terminal))

    await tool.execute(action="paste", session_id=session.id, text="print('hi')", bracketed=False)

    assert terminal.writes == ["print('hi')"]
```

Create `tests/framework/tools/terminal/backends/test_visible_windows_current.py` with a unit test around a helper function that extracts the last terminal segment from buffered text:

```python
from __future__ import annotations

from framework.tools.terminal.backends.visible_windows import extract_current_segment_from_buffer


def test_extract_current_segment_returns_empty_prompt() -> None:
    segment = extract_current_segment_from_buffer("Microsoft Windows\nC:\\repo>")

    assert segment.text == "C:\\repo>"
    assert segment.is_empty_prompt is True


def test_extract_current_segment_returns_last_command_to_now() -> None:
    text = "C:\\repo>git status\nOn branch main\nC:\\repo>npm"

    segment = extract_current_segment_from_buffer(text)

    assert segment.text == "C:\\repo>npm"
    assert segment.cursor_line == "C:\\repo>npm"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/test_process_tool_keys.py tests/framework/tools/terminal/backends/test_visible_windows_current.py -v
```

Expected: missing key/paste support and helper.

- [ ] **Step 3: Implement key encoding and visible current helper**

In `framework/tools/terminal/process_tool.py`, support:

```python
KEY_BYTES = {
    "enter": "\r",
    "escape": "\x1b",
    "ctrl+c": "\x03",
    "ctrl+d": "\x04",
    "tab": "\t",
    "backspace": "\x7f",
}
```

For `hex`, parse each token as a byte and concatenate `chr(int(token, 16))`.

In `framework/tools/terminal/backends/visible_windows.py`, add:

```python
def extract_current_segment_from_buffer(text: str) -> TerminalSegment:
    lines = text.splitlines()
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

Use this helper in `VisibleWindowsPtyBackend.current_segment()` with the backend's recent output buffer. If the host can provide a richer screen buffer, call the host first and fall back to the recent buffer.

- [ ] **Step 4: Run tests**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/test_process_tool_keys.py tests/framework/tools/terminal/backends/test_visible_windows_current.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add framework/tools/terminal/process_tool.py framework/tools/terminal/backends/visible_windows.py framework/tools/terminal/backends/visible_windows_host.py tests/framework/tools/terminal/test_process_tool_keys.py tests/framework/tools/terminal/backends/test_visible_windows_current.py
git commit -m "feat(terminal): support current segment and key input"
```

---

### Task 10: Windows Manager Factory

**Files:**
- Modify: `framework/tools/terminal/managers.py`
- Modify: `framework/tools/terminal/backends/factory.py`
- Modify: `framework/tools/terminal/__init__.py`
- Test: `tests/framework/tools/terminal/test_manager_factory.py`

- [ ] **Step 1: Write failing manager factory tests**

Create `tests/framework/tools/terminal/test_manager_factory.py`:

```python
from __future__ import annotations

import pytest

from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.managers import create_terminal_manager
from framework.tools.terminal.types import Platform, TerminalVisibility


def test_create_hidden_windows_manager() -> None:
    manager = create_terminal_manager(
        manager_kind="windows_hidden",
        shell="cmd",
        config=TerminalRuntimeConfig(),
    )

    assert manager.platform is Platform.WINDOWS
    assert manager.visibility is TerminalVisibility.HIDDEN


def test_create_visible_windows_manager() -> None:
    manager = create_terminal_manager(
        manager_kind="windows_visible",
        shell="cmd",
        config=TerminalRuntimeConfig(),
    )

    assert manager.platform is Platform.WINDOWS
    assert manager.visibility is TerminalVisibility.VISIBLE


def test_unknown_manager_kind_fails_loudly() -> None:
    with pytest.raises(ValueError, match="Unsupported terminal manager"):
        create_terminal_manager(manager_kind="linux_hidden", shell="bash", config=TerminalRuntimeConfig())
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/test_manager_factory.py -v
```

Expected: import failure for `create_terminal_manager`.

- [ ] **Step 3: Implement factory**

Add `WindowsHiddenTerminalManager` and `WindowsVisibleTerminalManager` as small subclasses of `BaseTerminalManager` that pass the right backend factory and visibility.

Add:

```python
def create_terminal_manager(
    *,
    manager_kind: str,
    shell: str,
    config: TerminalRuntimeConfig | None = None,
):
    ...
```

Supported first-phase values:

- `windows_hidden`
- `windows_visible`

Supported first-phase shell strings:

- `cmd`
- `powershell`
- `bash` only when detection resolves a Windows-accessible bash path.

Unknown manager or shell values must raise `ValueError` with a clear message.

- [ ] **Step 4: Run factory tests**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/test_manager_factory.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add framework/tools/terminal/managers.py framework/tools/terminal/backends/factory.py framework/tools/terminal/__init__.py tests/framework/tools/terminal/test_manager_factory.py
git commit -m "feat(terminal): add windows manager factory"
```

---

### Task 11: Bot Project and IOC Wiring

**Files:**
- Modify: `framework/ioc/configs/pool.py`
- Modify: `examples/bot_project/bot/service/pool_builder.py`
- Modify: `examples/bot_project/bot/service/builders.py`
- Modify: `examples/bot_project/bot/service/core.py`
- Modify: `examples/bot_project/config/pools/main.yml`
- Modify: `examples/bot_project/config/pools/coding.yml`
- Test: `examples/bot_project/tests/test_terminal_integration.py`

- [ ] **Step 1: Write failing bot integration test**

Modify `examples/bot_project/tests/test_terminal_integration.py` with tests:

```python
def test_pool_tool_manager_registers_command_process_terminal_for_configured_manager() -> None:
    from examples.bot_project.bot.service.pool_builder import _build_pool_tool_manager
    from framework.ioc.configs.pool import PoolConfig

    raw = {
        "llm": {"model": "fake", "api_key": "fake"},
        "terminal": {"manager": "windows_hidden", "shell": "cmd"},
        "agents": [{"name": "main", "role": "main", "standard_tools": True}],
    }
    pool_cfg = PoolConfig.model_validate(raw)

    assert pool_cfg.terminal.manager == "windows_hidden"
    assert pool_cfg.terminal.shell == "cmd"
```

Also update existing assertions so they expect `command` and `process`, and assert no registered tool is named `shell`.

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
python -m pytest examples/bot_project/tests/test_terminal_integration.py -v
```

Expected: `TerminalConfig` has no `manager` or `shell`, and builder still registers shell.

- [ ] **Step 3: Update IOC config**

Modify `framework/ioc/configs/pool.py`:

```python
class TerminalConfig(BaseModel):
    manager: str = "windows_hidden"
    shell: str = "cmd"
    storage_dir: str = "data/terminals"
    close_on_exit: bool = False
    max_terminals: int = 5
    default_yield_ms: int = 10_000
    default_command_timeout_seconds: int = 60
    command_tool_outer_timeout_seconds: int = 70
    input_wait_idle_ms: int = 15_000
    poll_max_wait_ms: int = 30_000
    max_output_chars: int = 200_000
    pending_max_output_chars: int = 30_000
    finished_ttl_ms: int = 1_800_000
```

- [ ] **Step 4: Update bot tool registration**

In `examples/bot_project/bot/service/pool_builder.py`:

- replace `_create_terminal_manager` with a call to `create_terminal_manager`;
- create one `ProcessRegistry` per pool;
- register `CommandTool`, `ProcessTool`, and `TerminalTool`;
- remove `_make_shell_tool` and `SubprocessExecutor` usage.

The tool registration must not branch on visible/hidden except inside the factory.

- [ ] **Step 5: Update YAML configs**

Change `examples/bot_project/config/pools/main.yml` and `coding.yml`:

```yaml
terminal:
  manager: "windows_visible"
  shell: "bash"
  storage_dir: "data/terminals/main"
  close_on_exit: false
  max_terminals: 5
  default_yield_ms: 10000
  default_command_timeout_seconds: 60
  command_tool_outer_timeout_seconds: 70
  input_wait_idle_ms: 15000
```

Update approval config from:

```yaml
shell: {allowed_paths: ["*"]}
```

to:

```yaml
command: {allowed_paths: ["*"]}
```

- [ ] **Step 6: Run bot tests**

Run:

```powershell
python -m pytest examples/bot_project/tests/test_terminal_integration.py examples/bot_project/tests/test_runtime_defaults.py -v
```

Expected: tests pass; update runtime default tests to assert command timeout envelope.

- [ ] **Step 7: Commit**

```powershell
git add framework/ioc/configs/pool.py examples/bot_project/bot/service/pool_builder.py examples/bot_project/bot/service/builders.py examples/bot_project/bot/service/core.py examples/bot_project/config/pools/main.yml examples/bot_project/config/pools/coding.yml examples/bot_project/tests/test_terminal_integration.py examples/bot_project/tests/test_runtime_defaults.py
git commit -m "feat(bot): wire command process terminal tools"
```

---

### Task 12: Approval and Legacy Shell Cleanup

**Files:**
- Modify: `framework/approval` or ReAct approval classifier files if tool names are hard-coded.
- Modify: `framework/tools/standard/__init__.py`
- Delete: `framework/tools/standard/shell_tool.py`
- Update tests that import `ShellTool`.
- Test: `tests/framework/tools/standard/test_shell_tool.py`
- Test: approval tests that mention shell.

- [ ] **Step 1: Find legacy shell imports**

Run:

```powershell
rg -n "ShellTool|SubprocessExecutor|TerminalSessionExecutor|tool_name=\"shell\"|shell:" framework examples tests
```

Expected: output lists all remaining shell references.

- [ ] **Step 2: Write/update failing approval tests**

Update the approval test that currently validates shell approval so it validates command approval instead. The assertion must check that `command` arguments use the `command` and `workdir` fields, not old `shell` fields.

Example assertion:

```python
assert request.tool_name == "command"
assert request.arguments["command"] == "git status"
```

- [ ] **Step 3: Remove legacy shell exports and file**

Remove `ShellTool`, `SubprocessExecutor`, and `TerminalSessionExecutor` from `framework/tools/standard/__init__.py`.

Delete `framework/tools/standard/shell_tool.py` after all imports are updated. If deleting causes unrelated old examples to fail, update those examples to use `CommandTool`.

- [ ] **Step 4: Update tests**

Delete or rewrite `tests/framework/tools/standard/test_shell_tool.py`:

- tests for dangerous command safety should move to command approval/classifier tests;
- tests for stateful terminal behavior should live in `test_command_tool.py`, `test_process_tool.py`, and backend tests.

- [ ] **Step 5: Run focused checks**

Run:

```powershell
python -m pytest tests/framework/tools/terminal tests/framework/tools/standard examples/bot_project/tests/test_terminal_integration.py -v
```

Expected: all focused tests pass.

- [ ] **Step 6: Commit**

```powershell
git add framework examples tests
git commit -m "refactor(terminal): remove legacy shell tool"
```

---

### Task 13: Windows Integration Tests

**Files:**
- Create: `tests/framework/tools/terminal/test_windows_command_integration.py`

- [ ] **Step 1: Write Windows integration tests**

Create `tests/framework/tools/terminal/test_windows_command_integration.py`:

```python
from __future__ import annotations

import sys

import pytest

from framework.tools.terminal.command_tool import CommandTool
from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.managers import create_terminal_manager
from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.process_tool import ProcessTool
from framework.tools.terminal.tool import TerminalTool


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only terminal integration")


@pytest.mark.asyncio
async def test_hidden_command_completes_without_subprocess_fallback() -> None:
    cfg = TerminalRuntimeConfig(default_command_timeout_seconds=5, command_tool_outer_timeout_seconds=10)
    manager = create_terminal_manager(manager_kind="windows_hidden", shell="cmd", config=cfg)
    registry = ProcessRegistry(config=cfg)
    tool = CommandTool(manager=manager, registry=registry, config=cfg)

    result = await tool.execute(command="echo hidden-ok", terminal="it")

    assert "status=completed" in result
    assert "hidden-ok" in result.lower()


@pytest.mark.asyncio
async def test_hidden_long_command_yields_and_can_be_killed() -> None:
    cfg = TerminalRuntimeConfig(default_yield_ms=10, default_command_timeout_seconds=30, command_tool_outer_timeout_seconds=40)
    manager = create_terminal_manager(manager_kind="windows_hidden", shell="cmd", config=cfg)
    registry = ProcessRegistry(config=cfg)
    command = CommandTool(manager=manager, registry=registry, config=cfg)
    process = ProcessTool(registry=registry, manager=manager)

    result = await command.execute(command="ping 127.0.0.1 -n 30", terminal="long", yield_ms=10)
    session_id = registry.list_running()[0].id
    killed = await process.execute(action="kill", session_id=session_id)

    assert "status=running" in result
    assert "killed" in killed.lower()


@pytest.mark.asyncio
async def test_terminal_current_reports_empty_prompt() -> None:
    cfg = TerminalRuntimeConfig()
    manager = create_terminal_manager(manager_kind="windows_hidden", shell="cmd", config=cfg)
    terminal = TerminalTool(manager)

    await terminal.execute(action="open", name="cur")
    current = await terminal.execute(action="current", name="cur")

    assert "Current terminal segment" in current
```

- [ ] **Step 2: Run Windows integration tests**

Run:

```powershell
python -m pytest tests/framework/tools/terminal/test_windows_command_integration.py -v
```

Expected on Windows: tests pass. Expected on non-Windows: skipped.

- [ ] **Step 3: Fix backend integration failures**

Fix only failures in backend lifecycle, command endings, or output draining. Do not reintroduce subprocess fallback.

- [ ] **Step 4: Commit**

```powershell
git add tests/framework/tools/terminal/test_windows_command_integration.py framework/tools/terminal
git commit -m "test(terminal): cover windows command integration"
```

---

### Task 14: Documentation Update

**Files:**
- Modify: `framework/tools/terminal/AGENTS.md`
- Modify: `docs/superpowers/specs/2026-05-25-terminal-command-redesign.md` if implementation decisions diverged.

- [ ] **Step 1: Update terminal AGENTS guide**

Rewrite the top-level architecture in `framework/tools/terminal/AGENTS.md` to describe:

- `command` starts commands;
- `process` manages running sessions;
- `terminal` manages tabs and current segment;
- visible/hidden are manager implementations;
- no subprocess fallback;
- Windows is the first implemented platform;
- `yield_ms` and `timeout` ordering.

- [ ] **Step 2: Run docs sanity check**

Run:

```powershell
rg -n "ShellTool|SubprocessExecutor|TerminalSessionExecutor|use the shell tool|shell tool" framework/tools/terminal/AGENTS.md docs/superpowers/specs/2026-05-25-terminal-command-redesign.md
```

Expected: no stale references except historical context explicitly marked as legacy.

- [ ] **Step 3: Commit**

```powershell
git add framework/tools/terminal/AGENTS.md docs/superpowers/specs/2026-05-25-terminal-command-redesign.md
git commit -m "docs(terminal): document command process tools"
```

---

## Final Verification

Run:

```powershell
python -m pytest tests/framework/tools/terminal -v
python -m pytest examples/bot_project/tests/test_terminal_integration.py examples/bot_project/tests/test_runtime_defaults.py -v
ruff check framework/tools/terminal examples/bot_project/bot/service tests/framework/tools/terminal
git status --short
```

Expected:

- terminal tests pass;
- bot terminal integration tests pass;
- ruff reports no issues;
- `git status --short` contains only unrelated pre-existing user files, if any.

## Self-Review

Spec coverage:

- Uniform tool semantics: Tasks 5, 6, 7, 10, 11.
- Visible/hidden manager decision: Tasks 4, 8, 9, 10.
- OS/shell layering: Tasks 1, 3, 10.
- OpenClaw process model: Tasks 2, 5, 6.
- `terminal current`: Tasks 7 and 9.
- timeout envelope and captured timeout output: Tasks 1, 5, 13.
- SSH/nested shell byte-stream behavior: Tasks 6 and 9.
- no subprocess fallback: Tasks 11 and 12.
- approval path remains existing architecture: Task 12.

Placeholder scan:

- The plan uses no `TBD`, `TODO`, `implement later`, or empty placeholder tasks.

Type consistency:

- Status enum is `ProcessStatus`.
- Registry type is `ProcessRegistry`.
- Manager factory is `create_terminal_manager`.
- Tool names are `command`, `process`, and `terminal`.
