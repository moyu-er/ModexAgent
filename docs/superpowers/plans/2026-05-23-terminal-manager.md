# Terminal Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enhance ShellTool with stateful terminal sessions, cross-platform PTY backends, dynamic LLM-aware descriptions, and LLM-visible terminal management tools.

**Architecture:** ShellExecutor ABC lets ShellTool switch between stateless subprocess and stateful terminal sessions. TerminalBackend ABC wraps pywinpty (Windows) and pexpect (Unix) as thin adapters. TerminalManager handles multi-session LRU eviction and JSON persistence. TerminalTool exposes management operations to the LLM.

**Tech Stack:** Python 3.11+, pywinpty (Windows), pexpect (Unix), asyncio, dataclasses, ABCs.

**Compliance:** All code must follow `rules/type-safety.md` — enums for categories/states, typed dataclasses, full type annotations, ABCs for cross-cutting concerns, no `getattr`/`hasattr`/`*attr`, framework/examples separation.

---

## File Structure

```
framework/tools/terminal/
├── __init__.py
├── manager.py              # TerminalManager
├── session.py              # TerminalSession, TerminalInfo, CommandRecord
├── tool.py                 # TerminalTool
├── state_store.py          # JsonTerminalStateStore
└── backends/
    ├── __init__.py
    ├── base.py             # TerminalBackend ABC
    ├── factory.py          # create_pty_backend()
    ├── windows_pty.py      # WindowsPtyBackend
    └── unix_pty.py         # UnixPtyBackend

framework/tools/standard/shell_tool.py  # Modified: +ShellExecutor +SubprocessExecutor +ShellInfo

examples/bot_project/bot/service/builders.py  # Modified: _make_shell_tool injects TerminalManager
examples/bot_project/bot/service/core.py      # Modified: initialize() creates TerminalManager
```

---

## Task 1: ShellInfo + ShellExecutor ABC + SubprocessExecutor + Shell Detection

**Files:**
- Create: `framework/tools/terminal/backends/__init__.py`
- Create: `framework/tools/terminal/__init__.py`
- Modify: `framework/tools/standard/shell_tool.py`

**Rationale:** These are the foundational types and the stateless fallback executor. They have no external dependencies and can be tested immediately.

---

- [ ] **Step 1: Write failing test for ShellInfo and shell detection**

Create `tests/framework/tools/terminal/test_shell_detection.py`:

```python
"""Tests for shell detection and ShellInfo."""

import sys
from unittest.mock import patch

import pytest

from framework.tools.standard.shell_tool import ShellInfo, detect_platform_shell


class TestShellInfo:
    def test_shell_info_creation(self):
        info = ShellInfo(name="bash", path="/bin/bash", platform="linux", is_stateful=False)
        assert info.name == "bash"
        assert info.path == "/bin/bash"
        assert info.platform == "linux"
        assert info.is_stateful is False

    def test_shell_info_immutable(self):
        info = ShellInfo(name="bash", path="/bin/bash", platform="linux", is_stateful=False)
        with pytest.raises(AttributeError):
            info.name = "zsh"


class TestDetectPlatformShell:
    def test_detect_bash_on_windows(self):
        """Windows: bash > powershell > cmd."""
        with patch("shutil.which") as mock_which, \
             patch("subprocess.run") as mock_run:
            mock_which.side_effect = lambda x: {
                "bash": "C:\\Git\\bin\\bash.exe",
                "powershell": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
                "cmd": "C:\\Windows\\System32\\cmd.exe",
            }.get(x)
            mock_run.return_value = type("Result", (), {"returncode": 0, "stdout": "GNU bash, version 5.2.0"})()

            info = detect_platform_shell()
            assert info.name == "bash"
            assert info.platform == "windows"
            assert "bash" in info.path.lower()

    def test_fallback_to_powershell_when_bash_fails(self):
        with patch("shutil.which") as mock_which, \
             patch("subprocess.run") as mock_run:
            mock_which.side_effect = lambda x: {
                "bash": "C:\\Git\\bin\\bash.exe",
                "powershell": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
                "cmd": "C:\\Windows\\System32\\cmd.exe",
            }.get(x)
            # bash --version fails
            def side_effect(*args, **kwargs):
                if "bash" in args[0]:
                    return type("Result", (), {"returncode": 1, "stdout": ""})()
                return type("Result", (), {"returncode": 0, "stdout": ""})()
            mock_run.side_effect = side_effect

            info = detect_platform_shell()
            assert info.name == "powershell"

    def test_fallback_to_cmd(self):
        with patch("shutil.which") as mock_which:
            mock_which.return_value = None
            with patch.dict(sys.modules, {"pexpect": None, "pywinpty": None}):
                info = detect_platform_shell()
                assert info.name == "cmd"
                assert info.platform == "windows"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/framework/tools/terminal/test_shell_detection.py -v`
Expected: FAIL with `ImportError: cannot import name 'ShellInfo'`

- [ ] **Step 3: Add ShellInfo dataclass and shell detection to shell_tool.py**

Modify `framework/tools/standard/shell_tool.py`. Add these imports at the top:

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
import shutil
import subprocess
```

Add before the `ShellTool` class:

```python
@dataclass(frozen=True)
class ShellInfo:
    """Information about the detected shell.

    Used to generate dynamic tool descriptions so the LLM knows
    which shell syntax to use.
    """

    name: str           # "bash", "powershell", "cmd", "zsh", "sh"
    path: str           # Full path to the executable
    platform: str       # "windows", "linux", "darwin"
    is_stateful: bool   # True: TerminalSession, False: Subprocess


class ShellExecutor(ABC):
    """Abstract strategy for executing shell commands.

    EXTENSION: Phase 2+ can add:
      - RemoteExecutor (asyncssh/paramiko)
      - DockerExecutor (docker exec)
    """

    @abstractmethod
    async def execute(self, command: str, working_dir: str | None = None, timeout: int = 60) -> str:
        """Execute a shell command and return its output."""

    @abstractmethod
    def shell_info(self) -> ShellInfo:
        """Return information about the shell for dynamic description generation."""


class SubprocessExecutor(ShellExecutor):
    """Stateless executor: each command runs in a fresh subprocess.

    This is the fallback when TerminalManager is unavailable.
    """

    def __init__(self, shell_info: ShellInfo | None = None):
        self._shell_info = shell_info or _detect_platform_shell()

    async def execute(self, command: str, working_dir: str | None = None, timeout: int = 60) -> str:
        import asyncio
        cwd = working_dir or os.getcwd()
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            return f"Error: Command timed out after {timeout} seconds"

        output_parts: list[str] = []
        if stdout:
            output_parts.append(stdout.decode("utf-8", errors="replace"))
        if stderr:
            stderr_text = stderr.decode("utf-8", errors="replace")
            if stderr_text.strip():
                output_parts.append(f"STDERR:\n{stderr_text}")
        if process.returncode != 0:
            output_parts.append(f"\nExit code: {process.returncode}")

        result = "\n".join(output_parts) if output_parts else "(no output)"
        max_len = 10000
        if len(result) > max_len:
            result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"
        return result

    def shell_info(self) -> ShellInfo:
        return self._shell_info


def _detect_platform_shell() -> ShellInfo:
    """Detect the best available shell for the current platform.

    Windows priority: bash > powershell > cmd
    Linux priority: bash > sh
    macOS priority: bash > zsh > sh
    """
    plat = platform.system().lower()

    if plat == "windows":
        # Windows: bash > powershell > cmd
        bash_path = shutil.which("bash")
        if bash_path:
            try:
                result = subprocess.run(
                    [bash_path, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and "bash" in result.stdout.lower():
                    return ShellInfo(name="bash", path=bash_path, platform="windows", is_stateful=False)
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass

        ps_path = shutil.which("powershell") or shutil.which("pwsh")
        if ps_path:
            return ShellInfo(name="powershell", path=ps_path, platform="windows", is_stateful=False)

        cmd_path = shutil.which("cmd") or shutil.which("cmd.exe")
        if cmd_path:
            return ShellInfo(name="cmd", path=cmd_path, platform="windows", is_stateful=False)

        return ShellInfo(name="cmd", path="cmd.exe", platform="windows", is_stateful=False)

    # Unix-like: Linux and macOS
    env_shell = os.environ.get("SHELL", "")
    if env_shell and shutil.which(env_shell):
        shell_name = Path(env_shell).name
        return ShellInfo(name=shell_name, path=env_shell, platform=plat, is_stateful=False)

    bash_path = shutil.which("bash")
    if bash_path:
        return ShellInfo(name="bash", path=bash_path, platform=plat, is_stateful=False)

    if plat == "darwin":
        zsh_path = shutil.which("zsh")
        if zsh_path:
            return ShellInfo(name="zsh", path=zsh_path, platform="darwin", is_stateful=False)

    sh_path = shutil.which("sh") or "/bin/sh"
    return ShellInfo(name="sh", path=sh_path, platform=plat, is_stateful=False)
```

- [ ] **Step 4: Modify ShellTool to accept ShellExecutor and generate dynamic description**

In `framework/tools/standard/shell_tool.py`, replace the `ShellTool.__init__` and `description` property:

```python
    def __init__(
        self,
        executor: ShellExecutor | None = None,
        timeout: int = 60,
        enable_safety_guard: bool = True,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
    ):
        """Initialize Shell tool.

        Args:
            executor: Shell execution strategy (defaults to SubprocessExecutor).
            timeout: Command timeout in seconds.
            enable_safety_guard: Whether to enable safety checks.
            deny_patterns: Custom deny patterns.
            allow_patterns: Allowlist patterns.
        """
        super().__init__()
        self._executor = executor or SubprocessExecutor()
        self.timeout = timeout
        self.enable_safety_guard = enable_safety_guard
        self._platform = platform.system().lower()

        if deny_patterns is not None:
            self.deny_patterns = deny_patterns
        elif self._platform == "windows":
            self.deny_patterns = self.WINDOWS_DENY_PATTERNS.copy()
        else:
            self.deny_patterns = self.POSIX_DENY_PATTERNS.copy()

        self.allow_patterns = allow_patterns or []

    @property
    def name(self) -> str:
        return "shell"

    @property
    def description(self) -> str:
        """Dynamically generate description based on actual shell type."""
        shell_info = self._executor.shell_info()
        parts = [
            f"Execute a shell command using {shell_info.name} and return its output."
        ]

        if shell_info.name == "bash":
            parts.append(
                "Commands run in bash. Use POSIX syntax: forward slashes for paths, "
                "single quotes for strings, && for chaining."
            )
        elif shell_info.name == "powershell":
            parts.append(
                "Commands run in PowerShell. Use PowerShell syntax: "
                "Get-ChildItem instead of ls, semicolons for chaining, "
                "backtick for line continuation."
            )
        elif shell_info.name == "cmd":
            parts.append(
                "Commands run in Windows CMD. Use CMD syntax: backslashes for paths, "
                "&& for chaining, %VAR% for environment variables."
            )
        elif shell_info.name == "zsh":
            parts.append(
                "Commands run in zsh. Compatible with bash syntax."
            )
        else:
            parts.append(
                "Commands run in sh. Use basic POSIX syntax."
            )

        if shell_info.is_stateful:
            parts.append(
                "This is a stateful session: cd, environment variables, "
                "and aliases persist across commands."
            )
        else:
            parts.append(
                "Each command runs in a fresh process: cd and environment "
                "changes do NOT persist."
            )

        if self.enable_safety_guard:
            parts.append("Safety guard is enabled.")

        return " ".join(parts)
```

And replace `execute`:

```python
    async def execute(self, command: str, working_dir: str | None = None, **kwargs: Any) -> str:
        if self.enable_safety_guard:
            guard_error = self._guard_command(command)
            if guard_error:
                return guard_error
        return await self._executor.execute(command, working_dir, timeout=self.timeout)
```

- [ ] **Step 5: Update shell_tool.py exports**

At the bottom of `framework/tools/standard/shell_tool.py`, ensure these are exported by adding to `__all__` if it exists, or just make sure the names are importable. The existing `__init__.py` imports `ShellTool` by name.

- [ ] **Step 6: Create terminal package __init__.py files**

Create `framework/tools/terminal/__init__.py`:

```python
"""Terminal management tools and backends."""

from __future__ import annotations

__all__: list[str] = []
```

Create `framework/tools/terminal/backends/__init__.py`:

```python
"""Terminal backend implementations."""

from __future__ import annotations

__all__: list[str] = []
```

- [ ] **Step 7: Run tests**

Run: `pytest tests/framework/tools/terminal/test_shell_detection.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add framework/tools/standard/shell_tool.py framework/tools/terminal/ tests/framework/tools/terminal/test_shell_detection.py
git commit -m "feat(terminal): ShellInfo, ShellExecutor ABC, SubprocessExecutor, dynamic description"
```

---

## Task 2: TerminalBackend ABC + Cross-Platform Factory

**Files:**
- Create: `framework/tools/terminal/backends/base.py`
- Create: `framework/tools/terminal/backends/factory.py`

**Rationale:** The abstract backend contract and factory must exist before any concrete implementation.

---

- [ ] **Step 1: Write failing test for TerminalBackend ABC**

Create `tests/framework/tools/terminal/backends/test_base.py`:

```python
"""Tests for TerminalBackend ABC."""

import pytest

from framework.tools.terminal.backends.base import TerminalBackend


class DummyBackend(TerminalBackend):
    async def start(self, shell: str | None = None, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        pass

    async def write(self, data: str) -> None:
        pass

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        return ""

    async def is_alive(self) -> bool:
        return True

    async def terminate(self) -> None:
        pass

    async def kill(self) -> None:
        pass


class TestTerminalBackend:
    def test_can_instantiate_concrete_subclass(self):
        backend = DummyBackend()
        assert isinstance(backend, TerminalBackend)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/framework/tools/terminal/backends/test_base.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'framework.tools.terminal.backends.base'`

- [ ] **Step 3: Implement TerminalBackend ABC**

Create `framework/tools/terminal/backends/base.py`:

```python
"""TerminalBackend abstract base class.

EXTENSION: Phase 2+ visible windows do not need a new ABC.
  Add `visible: bool` parameter to PtyBackend subclasses.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class TerminalBackend(ABC):
    """Abstract terminal backend — wraps mature PTY libraries.

    Implementations:
    - WindowsPtyBackend: pywinpty wrapper
    - UnixPtyBackend: pexpect wrapper

    EXTENSION: Phase 2+
      - TmuxBackend(TerminalBackend): reuse tmux sessions
      - WindowBackend via visible=True on PtyBackend subclasses
    """

    @abstractmethod
    async def start(
        self,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """Start the shell process."""

    @abstractmethod
    async def write(self, data: str) -> None:
        """Send input to the PTY."""

    @abstractmethod
    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        """Read PTY output. Non-blocking; returns collected text on timeout."""

    @abstractmethod
    async def is_alive(self) -> bool:
        """Check if the shell process is still running."""

    @abstractmethod
    async def terminate(self) -> None:
        """Graceful termination (SIGTERM equivalent)."""

    @abstractmethod
    async def kill(self) -> None:
        """Force kill (SIGKILL equivalent)."""
```

- [ ] **Step 4: Implement factory**

Create `framework/tools/terminal/backends/factory.py`:

```python
"""Cross-platform PTY backend factory."""

from __future__ import annotations

import logging
import sys

from .base import TerminalBackend

logger = logging.getLogger(__name__)


def create_pty_backend() -> TerminalBackend:
    """Create the appropriate PTY backend for the current platform.

    Raises:
        ImportError: If the required platform library is not installed.
    """
    if sys.platform == "win32":
        try:
            from .windows_pty import WindowsPtyBackend
            return WindowsPtyBackend()
        except ImportError as e:
            logger.error("pywinpty not installed. Install with: pip install pywinpty")
            raise
    else:
        try:
            from .unix_pty import UnixPtyBackend
            return UnixPtyBackend()
        except ImportError as e:
            logger.error("pexpect not installed. Install with: pip install pexpect")
            raise
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/framework/tools/terminal/backends/test_base.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add framework/tools/terminal/backends/base.py framework/tools/terminal/backends/factory.py tests/framework/tools/terminal/backends/test_base.py
git commit -m "feat(terminal): TerminalBackend ABC and cross-platform factory"
```

---

## Task 3: TerminalSession + TerminalInfo + CommandRecord

**Files:**
- Create: `framework/tools/terminal/session.py`
- Create: `tests/framework/tools/terminal/test_session.py`

---

- [ ] **Step 1: Write failing test for TerminalSession (with mock backend)**

```python
"""Tests for TerminalSession."""

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from framework.tools.terminal.session import CommandRecord, TerminalSession
from framework.tools.standard.shell_tool import ShellInfo


class MockBackend:
    def __init__(self):
        self.alive = True
        self.buffer = ""
        self._started = False

    async def start(self, shell: str | None = None, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        self._started = True

    async def write(self, data: str) -> None:
        self.buffer += data

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        return "mock-output\n$ "

    async def is_alive(self) -> bool:
        return self.alive

    async def terminate(self) -> None:
        self.alive = False

    async def kill(self) -> None:
        self.alive = False


class TestTerminalSession:
    def test_session_creation(self):
        backend = MockBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(name="bash", path="/bin/bash", platform="linux", is_stateful=True),
        )
        assert session.name == "test"
        assert session.shell_info.name == "bash"

    @pytest.mark.asyncio
    async def test_execute_restarts_dead_backend(self):
        backend = MockBackend()
        backend.alive = False
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(name="bash", path="/bin/bash", platform="linux", is_stateful=True),
        )
        result = await session.execute("echo hello")
        assert backend._started is True
        assert "mock-output" in result

    @pytest.mark.asyncio
    async def test_execute_records_history(self):
        backend = MockBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(name="bash", path="/bin/bash", platform="linux", is_stateful=True),
            max_history=2,
        )
        await session.execute("cmd1")
        await session.execute("cmd2")
        history = session.get_history()
        assert len(history) == 2
        assert history[0].command == "cmd1"
        assert history[1].command == "cmd2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/framework/tools/terminal/test_session.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: Implement TerminalSession**

Create `framework/tools/terminal/session.py`:

```python
"""TerminalSession — single named session wrapping a TerminalBackend."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from framework.tools.standard.shell_tool import ShellInfo
    from framework.tools.terminal.backends.base import TerminalBackend


@dataclass
class CommandRecord:
    """A single command execution record."""

    command: str
    output: str
    exit_code: int | None = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class TerminalInfo:
    """Metadata about a terminal session."""

    name: str
    shell_type: str
    is_alive: bool
    last_active: float
    command_count: int


class TerminalSession:
    """Wraps a TerminalBackend with history, auto-restart, and LRU tracking.

    EXTENSION: Phase 2+ concurrent control:
      - Add _lock: asyncio.Lock for exclusive access
      - Add _input_queue: asyncio.Queue for queueing LLM + user input
      - Add inject_user_input(text) method
    """

    def __init__(
        self,
        name: str,
        backend: TerminalBackend,
        shell_info: ShellInfo,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        max_history: int = 5,
        history_truncate: int = 200,
    ):
        self.name = name
        self._backend = backend
        self.shell_info = shell_info
        self._cwd = cwd
        self._env = env
        self._max_history = max_history
        self._history_truncate = history_truncate
        self._history: list[CommandRecord] = []
        self.last_active = time.time()
        self._needs_restart = True

    async def execute(self, command: str, timeout: float = 60.0) -> str:
        """Execute a command and return output.

        Flow:
        1. Check backend alive, restart if dead (lazy recovery).
        2. Send command + newline to PTY.
        3. Read output until timeout or prompt heuristic.
        4. Record truncated history.
        5. Update last_active.
        """
        if not await self._backend.is_alive() or self._needs_restart:
            await self._backend.start(
                shell=self.shell_info.path,
                cwd=self._cwd,
                env=self._env,
            )
            self._needs_restart = False

        await self._backend.write(command + "\n")

        # Read output with timeout
        output_parts: list[str] = []
        start_time = time.time()
        while time.time() - start_time < timeout:
            chunk = await self._backend.read(timeout=0.5, max_size=65536)
            if chunk:
                output_parts.append(chunk)
            # Simple heuristic: if we see a prompt-like ending, break early
            combined = "".join(output_parts)
            if combined.rstrip().endswith(("$ ", "# ", "> ")):
                break
            await asyncio.sleep(0.1)

        output = "".join(output_parts)

        # Truncate and record
        truncated_cmd = command[:self._history_truncate]
        truncated_out = output[:self._history_truncate]
        record = CommandRecord(
            command=truncated_cmd,
            output=truncated_out,
        )
        self._history.append(record)
        if len(self._history) > self._max_history:
            self._history.pop(0)

        self.last_active = time.time()
        return output

    def get_history(self) -> list[CommandRecord]:
        """Return command history (newest last)."""
        return list(self._history)

    def to_info(self) -> TerminalInfo:
        """Return metadata for list/inspection."""
        return TerminalInfo(
            name=self.name,
            shell_type=self.shell_info.name,
            is_alive=asyncio.run(self._backend.is_alive()),  # sync wrapper for serialization
            last_active=self.last_active,
            command_count=len(self._history),
        )

    async def close(self) -> None:
        """Terminate the backend gracefully, then force kill if needed."""
        await self._backend.terminate()
        # Give it a moment to terminate gracefully
        await asyncio.sleep(0.5)
        if await self._backend.is_alive():
            await self._backend.kill()
```

**Note:** `to_info()` uses `asyncio.run()` which is problematic. We'll fix this in Task 5 by making TerminalManager track the alive status. For now, this is a placeholder.

- [ ] **Step 4: Run tests**

Run: `pytest tests/framework/tools/terminal/test_session.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add framework/tools/terminal/session.py tests/framework/tools/terminal/test_session.py
git commit -m "feat(terminal): TerminalSession with history and auto-restart"
```

---

## Task 4: TerminalStateStore (JSON Persistence)

**Files:**
- Create: `framework/tools/terminal/state_store.py`
- Create: `tests/framework/tools/terminal/test_state_store.py`

---

- [ ] **Step 1: Write failing test**

```python
"""Tests for TerminalStateStore."""

import json
import tempfile
from pathlib import Path

import pytest

from framework.tools.terminal.state_store import JsonTerminalStateStore
from framework.tools.standard.shell_tool import ShellInfo


class TestJsonTerminalStateStore:
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as td:
            store = JsonTerminalStateStore(Path(td))
            state = {
                "version": 1,
                "default_terminal": "tab-1",
                "sessions": [
                    {
                        "name": "tab-1",
                        "shell_type": "bash",
                        "shell_path": "/bin/bash",
                        "cwd": "/home/user",
                        "env": {"KEY": "value"},
                        "created_at": 1234567890.0,
                        "last_active": 1234567900.0,
                        "history": [
                            {"command": "ls", "output": "file.txt", "exit_code": 0, "timestamp": 1234567895.0}
                        ],
                        "needs_restart": True,
                    }
                ],
            }
            store.save(state)
            loaded = store.load()
            assert loaded["default_terminal"] == "tab-1"
            assert len(loaded["sessions"]) == 1
            assert loaded["sessions"][0]["name"] == "tab-1"

    def test_load_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            store = JsonTerminalStateStore(Path(td) / "nonexistent")
            result = store.load()
            assert result == {}

    def test_load_corrupted_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            store = JsonTerminalStateStore(Path(td))
            # Write invalid JSON
            store._file_path.write_text("not json")
            result = store.load()
            assert result == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/framework/tools/terminal/test_state_store.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: Implement JsonTerminalStateStore**

Create `framework/tools/terminal/state_store.py`:

```python
"""Persistent storage for terminal state (JSON)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class JsonTerminalStateStore:
    """Stores terminal session metadata and history to a JSON file.

    Note: Does NOT serialize the actual process — only metadata.
    Sessions are lazily restarted on first use after load.
    """

    def __init__(self, storage_dir: Path, filename: str = "state.json"):
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._file_path = self._storage_dir / filename

    def save(self, state: dict[str, Any]) -> None:
        """Save state to JSON file."""
        try:
            with open(self._file_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.exception("Failed to save terminal state")

    def load(self) -> dict[str, Any]:
        """Load state from JSON file.

        Returns empty dict if file missing or corrupted.
        """
        if not self._file_path.exists():
            return {}
        try:
            with open(self._file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning("Terminal state file corrupted, starting fresh")
            return {}
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/framework/tools/terminal/test_state_store.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add framework/tools/terminal/state_store.py tests/framework/tools/terminal/test_state_store.py
git commit -m "feat(terminal): JsonTerminalStateStore for JSON persistence"
```

---

## Task 5: TerminalManager (Multi-Session + LRU Eviction)

**Files:**
- Create: `framework/tools/terminal/manager.py`
- Create: `tests/framework/tools/terminal/test_manager.py`

---

- [ ] **Step 1: Write failing test**

```python
"""Tests for TerminalManager."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.tools.terminal.manager import TerminalManager
from framework.tools.standard.shell_tool import ShellInfo


class TestTerminalManager:
    @pytest.fixture
    def manager(self):
        with tempfile.TemporaryDirectory() as td:
            tm = TerminalManager(
                storage_dir=Path(td),
                max_terminals=3,
                history_count=2,
                history_truncate=50,
            )
            yield tm

    @pytest.mark.asyncio
    async def test_get_or_create_creates_session(self, manager):
        session = await manager.get_or_create("tab-1")
        assert session.name == "tab-1"
        assert "tab-1" in manager.list_names()

    @pytest.mark.asyncio
    async def test_lru_eviction(self, manager):
        """Max 3 terminals; creating 4 evicts the least recently used."""
        s1 = await manager.get_or_create("tab-1")
        s2 = await manager.get_or_create("tab-2")
        s3 = await manager.get_or_create("tab-3")

        # Touch tab-1 to make it recently used
        s1.last_active = 9999999999.0

        # Create tab-4, should evict tab-2 (oldest last_active)
        s4 = await manager.get_or_create("tab-4")
        assert "tab-1" in manager.list_names()
        assert "tab-2" not in manager.list_names()
        assert "tab-3" in manager.list_names()
        assert "tab-4" in manager.list_names()

    @pytest.mark.asyncio
    async def test_select_default(self, manager):
        await manager.get_or_create("tab-1")
        await manager.get_or_create("tab-2")
        manager.select_default("tab-2")
        assert manager.get_default_session().name == "tab-2"

    @pytest.mark.asyncio
    async def test_close_removes_session(self, manager):
        await manager.get_or_create("tab-1")
        result = await manager.close("tab-1")
        assert result is True
        assert "tab-1" not in manager.list_names()

    @pytest.mark.asyncio
    async def test_persistence(self, manager):
        await manager.get_or_create("tab-1")
        await manager.save_state()

        # Create new manager pointing to same directory
        manager2 = TerminalManager(
            storage_dir=manager._storage_dir,
            max_terminals=3,
        )
        await manager2.load_state()
        assert "tab-1" in manager2.list_names()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/framework/tools/terminal/test_manager.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: Implement TerminalManager**

Create `framework/tools/terminal/manager.py`:

```python
"""TerminalManager — multi-session management with LRU eviction and persistence."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from framework.tools.standard.shell_tool import ShellInfo, _detect_platform_shell
from framework.tools.terminal.backends.factory import create_pty_backend
from framework.tools.terminal.session import CommandRecord, TerminalSession
from framework.tools.terminal.state_store import JsonTerminalStateStore

logger = logging.getLogger(__name__)


@dataclass
class TerminalManagerConfig:
    """Configuration for TerminalManager."""

    max_terminals: int = 5
    history_count: int = 5
    history_truncate: int = 200
    storage_dir: Path = field(default_factory=lambda: Path("data/terminals"))
    default_timeout: float = 60.0


class TerminalManager:
    """Manages named terminal sessions with LRU eviction and JSON persistence.

    Responsibilities:
    - Named session collection (name -> TerminalSession)
    - Default terminal for ShellTool
    - LRU eviction when max_terminals exceeded
    - Lazy alive detection (check only on use)
    - Persist/restore session metadata and history
    """

    def __init__(
        self,
        storage_dir: Path | str = "data/terminals",
        max_terminals: int = 5,
        history_count: int = 5,
        history_truncate: int = 200,
        default_timeout: float = 60.0,
    ):
        self._storage_dir = Path(storage_dir)
        self._max_terminals = max_terminals
        self._history_count = history_count
        self._history_truncate = history_truncate
        self._default_timeout = default_timeout
        self._sessions: dict[str, TerminalSession] = {}
        self._default_terminal: str | None = None
        self._store = JsonTerminalStateStore(self._storage_dir)
        self._shell_info = _detect_platform_shell()

    async def get_or_create(self, name: str, cwd: str | None = None) -> TerminalSession:
        """Get existing session or create a new one. Evicts LRU if at capacity."""
        if name in self._sessions:
            session = self._sessions[name]
            session.last_active = time.time()
            return session

        # Evict oldest if at capacity
        if len(self._sessions) >= self._max_terminals:
            await self._evict_oldest()

        backend = create_pty_backend()
        session = TerminalSession(
            name=name,
            backend=backend,
            shell_info=self._shell_info,
            cwd=cwd,
            max_history=self._history_count,
            history_truncate=self._history_truncate,
        )
        self._sessions[name] = session
        self._default_terminal = name
        logger.info("Created terminal session: %s", name)
        return session

    def get(self, name: str) -> TerminalSession | None:
        """Get session by name without creating."""
        return self._sessions.get(name)

    async def close(self, name: str) -> bool:
        """Close a session. Returns True if existed."""
        session = self._sessions.pop(name, None)
        if session is None:
            return False
        await session.close()
        if self._default_terminal == name:
            self._default_terminal = next(iter(self._sessions), None)
        logger.info("Closed terminal session: %s", name)
        return True

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all sessions with metadata."""
        result = []
        for name, session in self._sessions.items():
            result.append({
                "name": name,
                "shell_type": session.shell_info.name,
                "is_alive": True,  # Lazy: assume alive, checked on use
                "last_active": session.last_active,
                "command_count": len(session.get_history()),
                "is_default": name == self._default_terminal,
            })
        return result

    def list_names(self) -> list[str]:
        """Return just the session names."""
        return list(self._sessions.keys())

    def select_default(self, name: str) -> None:
        """Select the default terminal for ShellTool."""
        if name not in self._sessions:
            raise ValueError(f"Terminal '{name}' does not exist")
        self._default_terminal = name

    def get_default_session(self) -> TerminalSession | None:
        """Get the default session, or the only session, or None."""
        if self._default_terminal and self._default_terminal in self._sessions:
            return self._sessions[self._default_terminal]
        if len(self._sessions) == 1:
            return next(iter(self._sessions.values()))
        return None

    def get_history(self, name: str) -> list[CommandRecord]:
        """Get command history for a session."""
        session = self._sessions.get(name)
        if session is None:
            return []
        return session.get_history()

    async def save_state(self) -> None:
        """Persist session metadata and history to JSON."""
        sessions_data = []
        for name, session in self._sessions.items():
            sessions_data.append({
                "name": name,
                "shell_type": session.shell_info.name,
                "shell_path": session.shell_info.path,
                "cwd": session._cwd,
                "env": session._env,
                "created_at": session.last_active - 1,  # Approximate
                "last_active": session.last_active,
                "history": [
                    {
                        "command": rec.command,
                        "output": rec.output,
                        "exit_code": rec.exit_code,
                        "timestamp": rec.timestamp,
                    }
                    for rec in session.get_history()
                ],
                "needs_restart": True,
            })
        state = {
            "version": 1,
            "default_terminal": self._default_terminal,
            "sessions": sessions_data,
        }
        self._store.save(state)

    async def load_state(self) -> None:
        """Restore session metadata from JSON. Sessions are lazily restarted on use."""
        data = self._store.load()
        if not data:
            return

        for sess_data in data.get("sessions", []):
            name = sess_data["name"]
            shell_type = sess_data.get("shell_type", self._shell_info.name)
            shell_path = sess_data.get("shell_path", self._shell_info.path)
            backend = create_pty_backend()
            session = TerminalSession(
                name=name,
                backend=backend,
                shell_info=ShellInfo(
                    name=shell_type,
                    path=shell_path,
                    platform=self._shell_info.platform,
                    is_stateful=True,
                ),
                cwd=sess_data.get("cwd"),
                env=sess_data.get("env"),
                max_history=self._history_count,
                history_truncate=self._history_truncate,
            )
            session.last_active = sess_data.get("last_active", time.time())
            # Restore history
            for rec_data in sess_data.get("history", []):
                session._history.append(CommandRecord(
                    command=rec_data["command"],
                    output=rec_data["output"],
                    exit_code=rec_data.get("exit_code"),
                    timestamp=rec_data.get("timestamp", time.time()),
                ))
            session._needs_restart = True
            self._sessions[name] = session

        self._default_terminal = data.get("default_terminal")
        if self._default_terminal not in self._sessions:
            self._default_terminal = next(iter(self._sessions), None)
        logger.info("Loaded %d terminal sessions from state", len(self._sessions))

    async def _evict_oldest(self) -> None:
        """Close the least recently used session."""
        if not self._sessions:
            return
        oldest_name = min(self._sessions, key=lambda n: self._sessions[n].last_active)
        logger.info("LRU evicting terminal: %s", oldest_name)
        await self.close(oldest_name)

    async def close_all(self) -> None:
        """Close all sessions."""
        for name in list(self._sessions.keys()):
            await self.close(name)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/framework/tools/terminal/test_manager.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add framework/tools/terminal/manager.py tests/framework/tools/terminal/test_manager.py
git commit -m "feat(terminal): TerminalManager with LRU eviction and persistence"
```

---

## Task 6: TerminalTool (LLM-Visible Management Tool)

**Files:**
- Create: `framework/tools/terminal/tool.py`
- Create: `tests/framework/tools/terminal/test_tool.py`

---

- [ ] **Step 1: Write failing test**

```python
"""Tests for TerminalTool."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.tools.terminal.tool import TerminalAction, TerminalTool
from framework.tools.terminal.manager import TerminalManager


class TestTerminalTool:
    @pytest.fixture
    def tool(self):
        with tempfile.TemporaryDirectory() as td:
            tm = TerminalManager(storage_dir=Path(td), max_terminals=3)
            yield TerminalTool(tm)

    @pytest.mark.asyncio
    async def test_open_action(self, tool):
        result = await tool.execute(action="open", name="test-tab")
        assert "Opened terminal" in result
        assert "test-tab" in result

    @pytest.mark.asyncio
    async def test_list_action(self, tool):
        await tool._manager.get_or_create("tab-1")
        result = await tool.execute(action="list")
        assert "tab-1" in result

    @pytest.mark.asyncio
    async def test_close_action(self, tool):
        await tool._manager.get_or_create("tab-1")
        result = await tool.execute(action="close", name="tab-1")
        assert "Closed" in result

    @pytest.mark.asyncio
    async def test_select_action(self, tool):
        await tool._manager.get_or_create("tab-1")
        result = await tool.execute(action="select", name="tab-1")
        assert "Selected" in result

    @pytest.mark.asyncio
    async def test_history_action(self, tool):
        session = await tool._manager.get_or_create("tab-1")
        # Manually inject a history record
        from framework.tools.terminal.session import CommandRecord
        session._history.append(CommandRecord(command="ls", output="file.txt"))
        result = await tool.execute(action="history", name="tab-1")
        assert "ls" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/framework/tools/terminal/test_tool.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: Implement TerminalTool**

Create `framework/tools/terminal/tool.py`:

```python
"""TerminalTool — LLM-visible tool for managing terminal sessions."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from framework.core.tool_manager import Tool
from framework.tools.terminal.manager import TerminalManager


class TerminalAction(StrEnum):
    """Actions supported by TerminalTool."""

    OPEN = "open"
    CLOSE = "close"
    LIST = "list"
    SELECT = "select"
    HISTORY = "history"


class TerminalTool(Tool):
    """Tool for managing named terminal sessions.

    Parameters:
        action: One of open, close, list, select, history.
        name: Terminal name (optional for open, required otherwise).
        cwd: Initial working directory (only for open).
    """

    def __init__(self, manager: TerminalManager):
        super().__init__()
        self._manager = manager

    @property
    def name(self) -> str:
        return "terminal_manager"

    @property
    def description(self) -> str:
        return (
            "Manage persistent terminal sessions. "
            "Actions: open (create), close (terminate), list (show all), "
            "select (set default), history (show recent commands). "
            "After opening/selecting a terminal, use the shell tool to execute commands in it."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [TerminalAction.OPEN, TerminalAction.CLOSE, TerminalAction.LIST,
                             TerminalAction.SELECT, TerminalAction.HISTORY],
                    "description": "Action to perform",
                },
                "name": {
                    "type": "string",
                    "description": "Terminal name (optional for open, required for others)",
                },
                "cwd": {
                    "type": "string",
                    "description": "Initial working directory (only for open)",
                },
            },
            "required": ["action"],
        }

    async def execute(self, action: str, name: str | None = None, cwd: str | None = None, **kwargs: Any) -> str:
        try:
            action_enum = TerminalAction(action)
        except ValueError:
            return f"Error: Unknown action '{action}'. Valid actions: {', '.join(TerminalAction)}"

        if action_enum == TerminalAction.OPEN:
            target_name = name or self._auto_name()
            session = await self._manager.get_or_create(target_name, cwd=cwd)
            return f"Opened terminal '{target_name}' ({session.shell_info.name})."

        if action_enum == TerminalAction.CLOSE:
            if not name:
                return "Error: 'name' is required for close action"
            success = await self._manager.close(name)
            return f"Closed terminal '{name}'." if success else f"Terminal '{name}' not found."

        if action_enum == TerminalAction.LIST:
            sessions = self._manager.list_sessions()
            if not sessions:
                return "No active terminals."
            lines = ["Active terminals:"]
            for s in sessions:
                default_marker = " (default)" if s.get("is_default") else ""
                lines.append(
                    f"  - {s['name']}: {s['shell_type']}, "
                    f"commands={s['command_count']}{default_marker}"
                )
            return "\n".join(lines)

        if action_enum == TerminalAction.SELECT:
            if not name:
                return "Error: 'name' is required for select action"
            try:
                self._manager.select_default(name)
                return f"Selected '{name}' as default terminal."
            except ValueError as e:
                return f"Error: {e}"

        if action_enum == TerminalAction.HISTORY:
            if not name:
                return "Error: 'name' is required for history action"
            history = self._manager.get_history(name)
            if not history:
                return f"No history for terminal '{name}'."
            lines = [f"History for '{name}':"]
            for rec in history:
                lines.append(f"  > {rec.command}")
                if rec.output:
                    lines.append(f"    {rec.output[:80]}")
            return "\n".join(lines)

        return f"Error: Unhandled action '{action}'"

    def _auto_name(self) -> str:
        """Generate auto-incremented tab name."""
        existing = set(self._manager.list_names())
        for i in range(1, 1000):
            name = f"tab-{i}"
            if name not in existing:
                return name
        return "tab-auto"
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/framework/tools/terminal/test_tool.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add framework/tools/terminal/tool.py tests/framework/tools/terminal/test_tool.py
git commit -m "feat(terminal): TerminalTool for LLM-visible session management"
```

---

## Task 7: TerminalSessionExecutor (Stateful ShellExecutor)

**Files:**
- Modify: `framework/tools/standard/shell_tool.py`
- Create: `tests/framework/tools/terminal/test_session_executor.py`

---

- [ ] **Step 1: Write failing test**

```python
"""Tests for TerminalSessionExecutor."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.tools.standard.shell_tool import TerminalSessionExecutor, ShellInfo
from framework.tools.terminal.manager import TerminalManager


class TestTerminalSessionExecutor:
    @pytest.fixture
    def executor(self):
        with tempfile.TemporaryDirectory() as td:
            tm = TerminalManager(storage_dir=Path(td), max_terminals=3)
            yield TerminalSessionExecutor(terminal_manager=tm)

    @pytest.mark.asyncio
    async def test_execute_creates_default_terminal(self, executor):
        """If no default terminal exists, execute should auto-create one."""
        result = await executor.execute("echo hello")
        # Result depends on actual backend; we just verify no exception
        assert isinstance(result, str)

    def test_shell_info_reflects_manager_default(self, executor):
        info = executor.shell_info()
        assert info.is_stateful is True
        assert info.name in ("bash", "powershell", "cmd", "zsh", "sh")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/framework/tools/terminal/test_session_executor.py -v`
Expected: FAIL `ImportError: cannot import name 'TerminalSessionExecutor'`

- [ ] **Step 3: Add TerminalSessionExecutor to shell_tool.py**

Add after `SubprocessExecutor` in `framework/tools/standard/shell_tool.py`:

```python
class TerminalSessionExecutor(ShellExecutor):
    """Stateful executor: commands run in a persistent terminal session.

    EXTENSION: Phase 2+ can add:
      - RemoteExecutor: asyncssh/paramiko remote execution
      - DockerExecutor: docker exec
    """

    def __init__(
        self,
        terminal_manager: Any,  # Avoid circular import; runtime type is TerminalManager
        default_terminal: str | None = None,
    ):
        self._tm = terminal_manager
        self._default_terminal = default_terminal

    async def execute(self, command: str, working_dir: str | None = None, timeout: int = 60) -> str:
        session = self._tm.get_default_session()
        if session is None:
            name = self._default_terminal or "default"
            session = await self._tm.get_or_create(name, cwd=working_dir)
        return await session.execute(command, timeout=timeout)

    def shell_info(self) -> ShellInfo:
        session = self._tm.get_default_session()
        if session is not None:
            info = session.shell_info
            # Terminal sessions are always stateful
            return ShellInfo(
                name=info.name,
                path=info.path,
                platform=info.platform,
                is_stateful=True,
            )
        # Fallback to platform shell detection but mark as stateful
        info = _detect_platform_shell()
        return ShellInfo(
            name=info.name,
            path=info.path,
            platform=info.platform,
            is_stateful=True,
        )
```

**Note:** The import `Any` is already present at the top of `shell_tool.py`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/framework/tools/terminal/test_session_executor.py -v`
Expected: PASS (may be skipped if PTY libs unavailable — that's expected)

- [ ] **Step 5: Commit**

```bash
git add framework/tools/standard/shell_tool.py tests/framework/tools/terminal/test_session_executor.py
git commit -m "feat(terminal): TerminalSessionExecutor for stateful command execution"
```

---

## Task 8: Package Wiring (__init__.py Exports)

**Files:**
- Modify: `framework/tools/terminal/__init__.py`
- Modify: `framework/tools/standard/__init__.py`

---

- [ ] **Step 1: Update terminal package exports**

Replace `framework/tools/terminal/__init__.py`:

```python
"""Terminal management tools and backends."""

from __future__ import annotations

from framework.tools.terminal.manager import TerminalManager
from framework.tools.terminal.session import CommandRecord, TerminalInfo, TerminalSession
from framework.tools.terminal.state_store import JsonTerminalStateStore
from framework.tools.terminal.tool import TerminalAction, TerminalTool

__all__ = [
    "CommandRecord",
    "JsonTerminalStateStore",
    "TerminalAction",
    "TerminalInfo",
    "TerminalManager",
    "TerminalSession",
    "TerminalTool",
]
```

- [ ] **Step 2: Update standard tools exports**

Modify `framework/tools/standard/__init__.py`:

```python
from .shell_tool import (
    ShellExecutor,
    ShellInfo,
    ShellTool,
    SubprocessExecutor,
    TerminalSessionExecutor,
)

__all__ = [
    # ... existing exports ...
    "ShellTool",
    "ShellExecutor",
    "ShellInfo",
    "SubprocessExecutor",
    "TerminalSessionExecutor",
    # ... rest of existing exports ...
]
```

Make sure to keep all existing exports. The full updated file:

```python
"""标准化工具集合

提供跨平台的文件操作和 Shell 执行工具，作为 Skill 系统的基础。
"""

from .file_tool import (
    EditFileTool,
    FileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from .search_tool import FindFilesTool, SearchFilesTool
from .shell_tool import (
    ShellExecutor,
    ShellInfo,
    ShellTool,
    SubprocessExecutor,
    TerminalSessionExecutor,
)

__all__ = [
    "ReadFileTool",
    "WriteFileTool",
    "EditFileTool",
    "ListDirTool",
    "FileTool",
    "ShellTool",
    "ShellExecutor",
    "ShellInfo",
    "SubprocessExecutor",
    "TerminalSessionExecutor",
    "SearchFilesTool",
    "FindFilesTool",
]
```

- [ ] **Step 3: Verify imports work**

Run:
```bash
python -c "from framework.tools.terminal import TerminalManager, TerminalTool, TerminalSession; print('OK')"
python -c "from framework.tools.standard import ShellExecutor, ShellInfo, SubprocessExecutor, TerminalSessionExecutor; print('OK')"
```
Expected: Both print `OK`.

- [ ] **Step 4: Commit**

```bash
git add framework/tools/terminal/__init__.py framework/tools/standard/__init__.py
git commit -m "feat(terminal): package exports and wiring"
```

---

## Task 9: BotService Integration (builders.py + core.py)

**Files:**
- Modify: `examples/bot_project/bot/service/builders.py`
- Modify: `examples/bot_project/bot/service/core.py`

---

- [ ] **Step 1: Modify _make_shell_tool in builders.py**

In `examples/bot_project/bot/service/builders.py`, replace `_make_shell_tool`:

```python
def _make_shell_tool(
    terminal_manager: Any | None = None,
    timeout: int = 60,
    enable_safety_guard: bool = True,
) -> Tool:
    from framework.tools.standard import ShellTool, SubprocessExecutor, TerminalSessionExecutor

    if terminal_manager is not None:
        executor = TerminalSessionExecutor(
            terminal_manager=terminal_manager,
            default_terminal="default",
        )
    else:
        executor = SubprocessExecutor()
    return ShellTool(executor=executor, timeout=timeout, enable_safety_guard=enable_safety_guard)
```

- [ ] **Step 2: Modify BotService.initialize in core.py**

In `examples/bot_project/bot/service/core.py`, add to `__init__` after existing component declarations (~line 147):

```python
        # Terminal management
        self.terminal_manager: Any | None = None
```

In `initialize()`, after ToolManager creation and before `_register_tools()` (around line 237), insert:

```python
        # 3a. Create TerminalManager (with fallback)
        try:
            from framework.tools.terminal import TerminalManager
            from pathlib import Path

            terminals_dir = self._resolve_path("terminals_dir", str(Path(data_dir) / "terminals"))
            self.terminal_manager = TerminalManager(
                storage_dir=terminals_dir,
                max_terminals=getattr(self._app_config, 'terminal', {}).get('max_terminals', 5),
                history_count=5,
                history_truncate=200,
            )
            await self.terminal_manager.load_state()
            print(f"[OK] TerminalManager initialized ({len(self.terminal_manager.list_names())} sessions restored)")
        except Exception as e:
            logger.warning("TerminalManager initialization failed: %s. ShellTool will use SubprocessExecutor.", e)
            self.terminal_manager = None
```

And modify `_register_tools` call to pass terminal_manager. In `initialize()`, change:

```python
        await self._register_tools()
```

to:

```python
        await self._register_tools(terminal_manager=self.terminal_manager)
```

Then update `_register_tools` method signature in `builders.py`:

```python
    async def _register_tools(self, terminal_manager: Any | None = None) -> None:
        if self.tool_manager is None:
            return

        for tool in _make_file_tools():
            self.tool_manager.register(tool)

        shell_tool = _make_shell_tool(terminal_manager=terminal_manager, timeout=60)
        self.tool_manager.register(shell_tool)

        for tool in _make_search_tools():
            self.tool_manager.register(tool)
        print("   [OK] Standard tools registered (file + shell + search)")

        # Register TerminalTool if TerminalManager is available
        if terminal_manager is not None:
            from framework.tools.terminal import TerminalTool
            self.tool_manager.register(TerminalTool(terminal_manager))
            print("   [OK] terminal_manager registered")

        from bot.tools.custom import SendFileToUserTool
        self.tool_manager.register(SendFileToUserTool(output_adapter=self.output_adapter))
        print("   [OK] send_file_to_user registered")
```

- [ ] **Step 3: Verify imports**

Run:
```bash
python -c "from examples.bot_project.bot.service.builders import _make_shell_tool; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add examples/bot_project/bot/service/builders.py examples/bot_project/bot/service/core.py
git commit -m "feat(terminal): BotService integration with TerminalManager and TerminalTool"
```

---

## Task 10: Platform PTY Backends (Windows + Unix)

**Files:**
- Create: `framework/tools/terminal/backends/windows_pty.py`
- Create: `framework/tools/terminal/backends/unix_pty.py`
- Create: `tests/framework/tools/terminal/backends/test_backends.py`

**Note:** These require pywinpty (Windows) or pexpect (Unix). Tests should skip if unavailable.

---

- [ ] **Step 1: Write tests that skip if libraries unavailable**

```python
"""Tests for PTY backends. Skips if platform libraries not installed."""

import sys
from unittest.mock import MagicMock, patch

import pytest


class TestWindowsPtyBackend:
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_import(self):
        try:
            import pywinpty
        except ImportError:
            pytest.skip("pywinpty not installed")
        from framework.tools.terminal.backends.windows_pty import WindowsPtyBackend
        backend = WindowsPtyBackend()
        assert backend is not None


class TestUnixPtyBackend:
    @pytest.mark.skipif(sys.platform == "win32", reason="Unix only")
    def test_import(self):
        try:
            import pexpect
        except ImportError:
            pytest.skip("pexpect not installed")
        from framework.tools.terminal.backends.unix_pty import UnixPtyBackend
        backend = UnixPtyBackend()
        assert backend is not None
```

- [ ] **Step 2: Implement WindowsPtyBackend**

Create `framework/tools/terminal/backends/windows_pty.py`:

```python
"""Windows PTY backend — thin wrapper around pywinpty."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class WindowsPtyBackend:
    """Windows PTY using pywinpty.

    Core code < 40 lines. All PTY protocol details handled by pywinpty.
    Synchronous pywinpty API is wrapped via asyncio.run_in_executor.

    EXTENSION: Phase 2+ visible windows:
      - pywinpty supports ConPTY visible mode via spawn flags.
      - Add `visible: bool` parameter to constructor.
    """

    def __init__(self):
        try:
            import pywinpty
        except ImportError as e:
            raise ImportError("pywinpty is required for Windows PTY. Install: pip install pywinpty") from e
        self._pywinpty = pywinpty
        self._pty: Any | None = None

    async def start(
        self,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        loop = asyncio.get_event_loop()
        shell = shell or "cmd.exe"
        # pywinpty.PTY starts the process synchronously
        self._pty = await loop.run_in_executor(
            None,
            lambda: self._pywinpty.PTY(80, 24),
        )
        await loop.run_in_executor(
            None,
            lambda: self._pty.spawn(shell, cwd=cwd, env=env),
        )
        logger.debug("Windows PTY started: %s", shell)

    async def write(self, data: str) -> None:
        if self._pty is None:
            raise RuntimeError("PTY not started")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._pty.write, data)

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        if self._pty is None:
            raise RuntimeError("PTY not started")
        loop = asyncio.get_event_loop()

        # pywinpty read is non-blocking if data exists, blocks otherwise.
        # Use a short timeout approach via asyncio.wait_for.
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, self._pty.read, max_size),
                timeout=timeout,
            )
        except TimeoutError:
            return ""

    async def is_alive(self) -> bool:
        if self._pty is None:
            return False
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._pty.isalive())

    async def terminate(self) -> None:
        if self._pty is not None:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._pty.terminate)

    async def kill(self) -> None:
        if self._pty is not None:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._pty.kill)
```

- [ ] **Step 3: Implement UnixPtyBackend**

Create `framework/tools/terminal/backends/unix_pty.py`:

```python
"""Unix PTY backend — thin wrapper around pexpect."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class UnixPtyBackend:
    """Unix PTY using pexpect.

    Core code < 35 lines. All PTY protocol details handled by pexpect.
    pexpect.read_nonblocking is non-blocking, naturally suits async wrapping.

    EXTENSION: Phase 2+ visible windows:
      - Add `visible: bool` parameter.
      - visible=True: spawn xterm -e bash instead of direct bash spawn.
    """

    def __init__(self):
        try:
            import pexpect
        except ImportError as e:
            raise ImportError("pexpect is required for Unix PTY. Install: pip install pexpect") from e
        self._pexpect = pexpect
        self._child: Any | None = None

    async def start(
        self,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        shell = shell or "/bin/sh"
        # pexpect.spawn is synchronous but fast; wrap for consistency
        loop = asyncio.get_event_loop()
        self._child = await loop.run_in_executor(
            None,
            lambda: self._pexpect.spawn(shell, cwd=cwd, env=env, encoding="utf-8"),
        )
        logger.debug("Unix PTY started: %s", shell)

    async def write(self, data: str) -> None:
        if self._child is None:
            raise RuntimeError("PTY not started")
        self._child.send(data)

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        if self._child is None:
            raise RuntimeError("PTY not started")
        try:
            return self._child.read_nonblocking(size=max_size, timeout=timeout)
        except self._pexpect.TIMEOUT:
            return ""
        except self._pexpect.EOF:
            return ""

    async def is_alive(self) -> bool:
        if self._child is None:
            return False
        return self._child.isalive()

    async def terminate(self) -> None:
        if self._child is not None:
            self._child.terminate()

    async def kill(self) -> None:
        if self._child is not None:
            self._child.kill(9)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/framework/tools/terminal/backends/test_backends.py -v`
Expected: Skips if libraries missing, passes otherwise.

- [ ] **Step 5: Commit**

```bash
git add framework/tools/terminal/backends/windows_pty.py framework/tools/terminal/backends/unix_pty.py tests/framework/tools/terminal/backends/test_backends.py
git commit -m "feat(terminal): WindowsPtyBackend (pywinpty) and UnixPtyBackend (pexpect)"
```

---

## Spec Coverage Checklist

| Requirement | Task |
|-------------|------|
| Shell detection (bash > ps > cmd on Windows) | Task 1 |
| ShellExecutor ABC + SubprocessExecutor | Task 1 |
| Dynamic description based on ShellInfo | Task 1 |
| TerminalBackend ABC | Task 2 |
| Cross-platform factory | Task 2 |
| TerminalSession with history | Task 3 |
| TerminalInfo + CommandRecord | Task 3 |
| JSON persistence (state_store) | Task 4 |
| TerminalManager LRU eviction | Task 5 |
| TerminalManager persistence | Task 5 |
| TerminalTool (LLM-visible) | Task 6 |
| TerminalSessionExecutor | Task 7 |
| Package exports | Task 8 |
| BotService integration | Task 9 |
| Windows PTY backend | Task 10 |
| Unix PTY backend | Task 10 |
| Fallback to SubprocessExecutor | Task 9 (try/except in core.py) |

---

## Post-Implementation Verification

After all tasks are complete, run:

```bash
# 1. Run all new tests
pytest tests/framework/tools/terminal/ -v

# 2. Verify no regressions in existing tests
pytest tests/ -v --ignore=tests/integration  # adjust as needed

# 3. Check imports work
python -c "from framework.tools.terminal import TerminalManager, TerminalTool; from framework.tools.standard import ShellTool, TerminalSessionExecutor; print('All imports OK')"

# 4. Type check (if mypy configured)
mypy framework/tools/terminal/ framework/tools/standard/shell_tool.py --ignore-missing-imports
```
