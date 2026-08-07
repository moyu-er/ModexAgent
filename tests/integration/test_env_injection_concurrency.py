"""End-to-end integration tests for env-injection concurrency safety.

Verifies the "并发安全验证 场景 1" design from
``docs/design/task-workflow/env-injection.md`` (lines 174-208): the core
safety property that contextvar-based env injection does NOT
cross-contaminate between concurrent asyncio.Tasks.

Four test groups:

1. **并发隔离 (SubprocessExecutor)** — two concurrent asyncio.Tasks each
   set ``_modex_env`` internally and call ``SubprocessExecutor.execute()``.
   Each task's subprocess receives its own env; no cross-talk.

2. **并发隔离 (CommandTool)** — two concurrent asyncio.Tasks each set
   ``_current_session_id`` + ``_modex_env`` internally and call
   ``CommandTool.execute()`` on a shared ``BaseTerminalManager``. Two
   independent sessions are created; each backend's ``start()`` receives
   different env.

3. **fallback 安全** — without any contextvar set, both SubprocessExecutor
   and CommandTool behave exactly as before the env-injection wiring:
   env equals ``build_full_env()`` (no MODEX_ vars), CommandTool uses
   ``get_default()`` + ``env=None``.

4. **external agent 不变** — the external env builder
   (``tests/unit/agents/external/test_env_builder.py``) is zero-
   regression. This verification point is covered by Todo 2; this test
   only declares the coverage delegation in its docstring and asserts the
   referenced file exists. It does NOT re-run the unit tests.

Core property under test
------------------------
``asyncio.create_task`` (used internally by ``asyncio.gather``) copies the
current context at task-creation time. Setting a ``ContextVar`` inside
Task A modifies Task A's context copy ONLY — Task B's context copy is
unaffected. This per-task isolation is what makes env-injection safe for
concurrent task execution: each task reads its own ``_modex_env`` /
``_current_session_id`` values, and the tool instances (shared but
stateless) route env to the correct subprocess / terminal session.

Constraints (per spec)
----------------------
- No real PTY backend (platform-dependent) — uses ``_FakeBackend``.
- No hook dependency (Phase 2 DEFER) — tests set contextvars directly,
  simulating what the env-injection hook will do.
- No modexctl test (covered by Todo 3).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modex_agent.runtime.env_context import _current_session_id, _modex_env
from modex_agent.tools.terminal.backends.base import TerminalBackend
from modex_agent.tools.terminal.command_tool import CommandTool
from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.env import build_full_env
from modex_agent.tools.terminal.managers import BaseTerminalManager
from modex_agent.tools.terminal.poll_loop import PollOutcome, PollResult
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.results import TerminalRead, TerminalSegment
from modex_agent.tools.terminal.subprocess_tool import create_subprocess_executor
from modex_agent.tools.terminal.types import (
    Platform,
    ShellFamily,
    ShellInfo,
    TerminalVisibility,
)

pytestmark = pytest.mark.integration

# ── Fakes & helpers ──
# Pattern copied from tests/unit/tools/test_command_tool_env.py and
# tests/unit/tools/test_subprocess_env.py (spec permits copy over import).


def _make_fake_process(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    """Build a fake asyncio.subprocess.Process-like object.

    ``communicate`` is an ``AsyncMock`` because ``execute()`` awaits it.
    """
    process = MagicMock()
    process.communicate = AsyncMock(return_value=(stdout, stderr))
    process.returncode = returncode
    return process


@dataclass
class _StartCall:
    """Recorded invocation of ``TerminalBackend.start()``."""

    shell: str | None
    cwd: str | None
    env: dict[str, str] | None


class _FakeBackend(TerminalBackend):
    """Minimal ``TerminalBackend`` fake that records ``start()`` calls.

    Never produces real output; tests that exercise the poll loop mock
    ``poll_until_settled`` directly so no real PTY interaction is required.
    """

    def __init__(self) -> None:
        super().__init__()
        self._alive = False
        self.start_calls: list[_StartCall] = []
        self.writes: list[str] = []

    @property
    def platform(self) -> Platform:
        return Platform.LINUX

    @property
    def visibility(self) -> TerminalVisibility:
        return TerminalVisibility.HIDDEN

    async def start(
        self,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.start_calls.append(_StartCall(shell=shell, cwd=cwd, env=env))
        self._alive = True

    def _shell_family(self) -> ShellFamily:
        return ShellFamily.BASH

    async def interrupt(self) -> None:
        pass

    async def is_alive(self) -> bool:
        return self._alive

    async def terminate(self) -> None:
        self._alive = False

    async def kill(self) -> None:
        self._alive = False

    def stdin_writable(self) -> bool:
        return True

    async def write(self, data: str) -> None:
        self.writes.append(data)

    async def read_pending(self, timeout: float = 5.0, max_size: int = 65536) -> TerminalRead:
        return TerminalRead()

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        return ""

    async def drain_startup(self) -> None:
        pass

    async def current_segment(self) -> TerminalSegment:
        return TerminalSegment(text="")


_SHELL_INFO = ShellInfo(
    family=ShellFamily.BASH,
    path="/bin/bash",
    platform=Platform.LINUX,
)


def _make_backend_collecting_manager() -> tuple[BaseTerminalManager, list[_FakeBackend]]:
    """Build a manager whose ``backend_factory`` creates a fresh ``_FakeBackend``
    per call and appends it to the returned list.

    Tests inspect the list (or look up sessions by name) after execution to
    see exactly which backend each session received — necessary for the
    concurrency test where session creation order is non-deterministic.
    """
    backends: list[_FakeBackend] = []

    def _factory() -> TerminalBackend:
        b = _FakeBackend()
        backends.append(b)
        return b

    manager = BaseTerminalManager(
        shell_info=_SHELL_INFO,
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=_factory,
    )
    return manager, backends


@pytest.fixture
def _clean_contextvars() -> Iterator[None]:
    """Ensure ``_modex_env`` and ``_current_session_id`` are None before/after.

    Defensive net: tests that set these contextvars reset them in their own
    try/finally (or set them inside asyncio.Tasks whose context copies are
    discarded on completion). This fixture catches any leak from a prior test.
    """
    assert _modex_env.get() is None
    assert _current_session_id.get() is None
    yield None
    assert _modex_env.get() is None
    assert _current_session_id.get() is None


# ── Group 1: 并发隔离 (SubprocessExecutor) ──


async def test_subprocess_executor_concurrent_isolation(
    _clean_contextvars: None,
) -> None:
    """Two concurrent asyncio.Tasks each set ``_modex_env`` and call
    ``SubprocessExecutor.execute()``.

    Core property: ``asyncio.gather`` creates Tasks (via ``create_task``)
    which copy the current context at creation time. Setting ``_modex_env``
    inside Task A modifies Task A's context copy ONLY — Task B's copy is
    unaffected. Each task's subprocess receives its own env; no cross-talk.

    Verifies design doc "并发安全验证 场景 1" (env-injection.md:176-190):
    ``SubprocessTool → _modex_env.get()=env_X → subprocess(env=env_X)``.
    """
    fake_process = _make_fake_process()
    mock_create = AsyncMock(return_value=fake_process)

    async def task_a() -> None:
        token = _modex_env.set({"MODEX_TASK_ID": "task-a"})
        await asyncio.sleep(0)
        try:
            executor = create_subprocess_executor()
            await executor.execute("dummy-a")
        finally:
            _modex_env.reset(token)

    async def task_b() -> None:
        token = _modex_env.set({"MODEX_TASK_ID": "task-b"})
        await asyncio.sleep(0)
        try:
            executor = create_subprocess_executor()
            await executor.execute("dummy-b")
        finally:
            _modex_env.reset(token)

    with patch(
        "modex_agent.tools.terminal.subprocess_tool.asyncio.create_subprocess_exec",
        new=mock_create,
    ):
        await asyncio.gather(task_a(), task_b())

    # Both calls recorded — true concurrency via gather.
    assert mock_create.await_count == 2
    envs_passed: list[dict[str, str]] = [call.kwargs["env"] for call in mock_create.call_args_list]

    # Each task's env carries its own MODEX_TASK_ID — no cross-contamination.
    env_a = next(e for e in envs_passed if e.get("MODEX_TASK_ID") == "task-a")
    env_b = next(e for e in envs_passed if e.get("MODEX_TASK_ID") == "task-b")
    assert env_a["MODEX_TASK_ID"] == "task-a"
    assert env_b["MODEX_TASK_ID"] == "task-b"

    # Exactly one MODEX_ var per env (no leakage from the other task).
    assert [k for k in env_a if k.startswith("MODEX_")] == ["MODEX_TASK_ID"]
    assert [k for k in env_b if k.startswith("MODEX_")] == ["MODEX_TASK_ID"]

    # Main context untouched — tasks' context copies were discarded on exit.
    assert _modex_env.get() is None


# ── Group 2: 并发隔离 (CommandTool) ──


async def test_command_tool_concurrent_isolation(
    _clean_contextvars: None,
) -> None:
    """Two concurrent asyncio.Tasks each set ``_current_session_id`` +
    ``_modex_env`` and call ``CommandTool.execute()`` on a shared
    ``BaseTerminalManager``.

    Each Task's contextvar copy is isolated (``asyncio.create_task`` copies
    context). Task A uses sid-a + task-a, Task B uses sid-b + task-b. Both
    share the same ``BaseTerminalManager`` + ``CommandTool`` instance (tools
    are stateless — safe to share per design doc: "工具实例共享但无状态").

    After concurrent execution:
    - Two independent sessions exist (name="sid-a" and name="sid-b")
    - Each session's backend received different env in ``start()``

    Verifies design doc "并发安全验证 场景 1" (env-injection.md:183-189):
    ``CommandTool → get_or_create(sid_X) → tab_X → start(env=env_X)``.
    """
    manager, _backends = _make_backend_collecting_manager()
    registry = ProcessRegistry()
    tool = CommandTool(
        manager=manager,
        registry=registry,
        config=TerminalRuntimeConfig(),
    )

    fake_result = PollResult(
        outcome=PollOutcome.PROMPT_DETECTED,
        output_parts=["done"],
        elapsed_ms=5,
    )

    async def task_a() -> None:
        token_env = _modex_env.set({"MODEX_TASK_ID": "task-a"})
        token_sid = _current_session_id.set("sid-a")
        await asyncio.sleep(0)
        try:
            await tool.execute(command="echo a")
        finally:
            _modex_env.reset(token_env)
            _current_session_id.reset(token_sid)

    async def task_b() -> None:
        token_env = _modex_env.set({"MODEX_TASK_ID": "task-b"})
        token_sid = _current_session_id.set("sid-b")
        await asyncio.sleep(0)
        try:
            await tool.execute(command="echo b")
        finally:
            _modex_env.reset(token_env)
            _current_session_id.reset(token_sid)

    # Single patch — both tasks share the mock (same fake result for both).
    with patch(
        "modex_agent.tools.terminal.poll_loop.poll_until_settled",
        new=AsyncMock(return_value=fake_result),
    ):
        await asyncio.gather(task_a(), task_b())

    # Two independent sessions created by concurrent tasks.
    session_a = manager.get("sid-a")
    session_b = manager.get("sid-b")
    assert session_a is not None
    assert session_b is not None
    assert session_a.name == "sid-a"
    assert session_b.name == "sid-b"
    assert session_a is not session_b

    # Each session's backend received the correct env in start().
    # Private access: concurrent creation makes list-order → session mapping non-deterministic.
    backend_a = session_a._backend
    backend_b = session_b._backend
    assert isinstance(backend_a, _FakeBackend)
    assert isinstance(backend_b, _FakeBackend)
    assert backend_a.start_calls, "expected start() on sid-a backend"
    assert backend_b.start_calls, "expected start() on sid-b backend"

    env_a = backend_a.start_calls[0].env
    env_b = backend_b.start_calls[0].env
    assert env_a is not None
    assert env_b is not None
    assert env_a["MODEX_TASK_ID"] == "task-a"
    assert env_b["MODEX_TASK_ID"] == "task-b"
    # Different envs — no cross-contamination.
    assert env_a != env_b

    # Main context untouched — tasks' context copies were discarded.
    assert _modex_env.get() is None
    assert _current_session_id.get() is None


# ── Group 3: fallback 安全 ──


async def test_subprocess_executor_fallback_no_contextvar(
    _clean_contextvars: None,
) -> None:
    """Without ``_modex_env`` set, ``SubprocessExecutor`` behaves exactly as
    before the env-injection wiring: env equals ``build_full_env()`` with no
    MODEX_ vars.

    Pins the zero-behaviour-change invariant for the subprocess path.
    """
    fake_process = _make_fake_process()
    mock_create = AsyncMock(return_value=fake_process)

    with patch(
        "modex_agent.tools.terminal.subprocess_tool.asyncio.create_subprocess_exec",
        new=mock_create,
    ):
        executor = create_subprocess_executor()
        await executor.execute("echo hi")

    assert mock_create.await_count == 1
    env_passed = mock_create.call_args.kwargs["env"]
    expected = build_full_env()
    expected["NO_COLOR"] = "1"
    assert env_passed == expected
    assert not any(k.startswith("MODEX_") for k in env_passed)


async def test_command_tool_fallback_no_contextvar(
    _clean_contextvars: None,
) -> None:
    """Without contextvars, ``CommandTool`` uses ``get_default()`` +
    ``env=None``.

    Behavior identical to before the env-injection wiring:
    - session name is "default" (from ``get_default`` →
      ``get_or_create("default")``)
    - ``ensure_started`` receives ``env=None`` (``session._env`` stays None)
    - backend ``start()`` env has no MODEX_ vars

    Pins the zero-behaviour-change invariant for the CommandTool path.
    """
    manager, backends = _make_backend_collecting_manager()
    registry = ProcessRegistry()
    tool = CommandTool(
        manager=manager,
        registry=registry,
        config=TerminalRuntimeConfig(),
    )

    fake_result = PollResult(
        outcome=PollOutcome.PROMPT_DETECTED,
        output_parts=["hi"],
        elapsed_ms=5,
    )
    with patch(
        "modex_agent.tools.terminal.poll_loop.poll_until_settled",
        new=AsyncMock(return_value=fake_result),
    ):
        await tool.execute(command="echo hi")

    # Session created via get_default() → get_or_create("default").
    assert len(backends) == 1
    session = manager.get("default")
    assert session is not None
    assert session.name == "default"

    backend = backends[0]
    assert backend.start_calls, "expected start() to be called"
    start_env = backend.start_calls[0].env
    assert start_env is not None
    assert not any(k.startswith("MODEX_") for k in start_env)
    # env=None passed to ensure_started → self._env not mutated (stays None).
    assert session._env is None


# ── Group 4: external agent 不变 (declared, covered by Todo 2) ──


def test_external_agent_env_builder_coverage_declared() -> None:
    """External-coding env builder is zero-regression.

    This verification point is covered by Todo 2:
    ``tests/unit/agents/external/test_env_builder.py``.

    Per spec: "已在 Todo 2 覆盖, 此处不重复测试, 只在 docstring 中声明
    此验证点由 Todo 2 覆盖." This test does NOT re-run the unit tests — it
    only asserts the referenced file exists (structural check) and declares
    the coverage delegation via this docstring.

    To verify zero-regression, run::

        pytest tests/unit/agents/external/test_env_builder.py -v
    """
    referenced = (
        Path(__file__).resolve().parent.parent
        / "unit"
        / "agents"
        / "external"
        / "test_env_builder.py"
    )
    assert referenced.exists(), (
        f"Expected Todo 2 output at {referenced} — external agent env builder "
        "unit tests must exist. Run Todo 2 first."
    )
