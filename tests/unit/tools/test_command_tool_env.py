"""Tests for CommandTool env-injection + TerminalSession.ensure_started(env=).

Verifies the six acceptance criteria from the C3 spec:

1. ``test_ensure_started_env_mutates_self_env`` -- env param mutates
   ``self._env`` before start, so ``start()`` sees the injected env.
2. ``test_ensure_started_no_env_backward_compat`` -- no-arg call is unchanged.
3. ``test_ensure_started_idempotent`` -- second call with a different env
   does not trigger a second ``start()``.
4. ``test_ensure_backend_alive_uses_mutated_env`` -- after env injection,
   the recovery path (``_ensure_backend_alive``) re-starts with the same
   env.
5. ``test_command_tool_injects_env_from_contextvar`` -- CommandTool reads
   ``_modex_env`` and passes env to ``ensure_started``. Always uses
   ``get_default()`` for tab selection (not ``_current_session_id``).
6. ``test_command_tool_fallback_no_contextvar`` -- without contextvars,
   CommandTool falls back to ``get_default()`` + ``ensure_started(env=None)``.

The fake backend follows the pattern in
``tests/framework/tools/terminal/conftest.py`` but is minimal: it records
``start()`` calls and never produces real output. Tests that exercise the
poll loop mock ``poll_until_settled`` directly so no real PTY interaction
is required.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from modex_agent.runtime.env_context import _current_session_id, _modex_env
from modex_agent.tools.terminal.backends.base import TerminalBackend
from modex_agent.tools.terminal.command_tool import CommandTool
from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.managers import BaseTerminalManager
from modex_agent.tools.terminal.poll_loop import PollOutcome, PollResult
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.results import TerminalRead, TerminalSegment
from modex_agent.tools.terminal.session import TerminalSession
from modex_agent.tools.terminal.types import (
    Platform,
    ShellFamily,
    ShellInfo,
    TerminalVisibility,
)


@dataclass
class _StartCall:
    """Recorded invocation of ``TerminalBackend.start()``."""

    shell: str | None
    cwd: str | None
    env: dict[str, str] | None


class _FakeBackend(TerminalBackend):
    """Minimal ``TerminalBackend`` fake that records ``start()`` calls.

    Designed for env-injection tests: never produces real output. Tests
    that exercise the poll loop mock ``poll_until_settled`` directly so
    no real PTY interaction is required. The ``is_alive`` flag is
    mutable so tests can simulate backend death for the recovery path
    (``_ensure_backend_alive``).
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


def _make_session(
    *,
    name: str = "test",
    env: dict[str, str] | None = None,
    backend: _FakeBackend | None = None,
) -> tuple[TerminalSession, _FakeBackend]:
    """Build a session backed by a fresh (or provided) ``_FakeBackend``."""
    b = backend if backend is not None else _FakeBackend()
    session = TerminalSession(
        name=name,
        backend=b,
        shell_info=_SHELL_INFO,
        env=env,
    )
    return session, b


def _make_manager(backends: list[_FakeBackend]) -> BaseTerminalManager:
    """Build a manager whose ``backend_factory`` pops from ``backends`` (FIFO).

    Each call to the factory returns the next pre-built fake backend, so
    tests can inspect the exact backend instance a session received.
    """

    def _factory() -> TerminalBackend:
        if backends:
            return backends.pop(0)
        return _FakeBackend()

    return BaseTerminalManager(
        shell_info=_SHELL_INFO,
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=_factory,
    )


def _make_config() -> TerminalRuntimeConfig:
    return TerminalRuntimeConfig()


@pytest.fixture
def _clean_contextvars() -> Iterator[None]:
    """Ensure ``_modex_env`` and ``_current_session_id`` are None during the test.

    Tests that set these contextvars reset them in their own try/finally;
    this fixture is a defensive net for tests that don't, so a leak from
    a prior test cannot pollute the current one.
    """
    assert _modex_env.get() is None
    assert _current_session_id.get() is None
    yield None
    assert _modex_env.get() is None
    assert _current_session_id.get() is None


# ── TerminalSession.ensure_started(env=...) ──


async def test_ensure_started_env_mutates_self_env(
    _clean_contextvars: None,
) -> None:
    """``ensure_started(env=...)`` mutates ``self._env`` before calling ``start()``.

    Verifies the CRITICAL mutate pattern: the injected env appears both
    in the backend's ``start()`` call AND in ``session._env`` (so the
    recovery path ``_ensure_backend_alive``, which reads
    ``self._startup_env()`` -> ``self._env``, also uses the injected env).
    """
    session, backend = _make_session(env=None)
    await session.ensure_started(env={"MODEX_TASK_ID": "t1"})

    assert len(backend.start_calls) == 1
    assert backend.start_calls[0].env is not None
    assert backend.start_calls[0].env["MODEX_TASK_ID"] == "t1"
    assert session._env == {"MODEX_TASK_ID": "t1"}


async def test_ensure_started_no_env_backward_compat(
    _clean_contextvars: None,
) -> None:
    """``ensure_started()`` with no args behaves exactly as before.

    ``self._env`` is NOT mutated (stays whatever it was -- None here),
    and ``start()`` still receives ``build_full_env(self._env)`` from
    ``_startup_env()``.
    """
    session, backend = _make_session(env=None)
    await session.ensure_started()

    assert len(backend.start_calls) == 1
    # build_full_env(None) still returns a dict (os.environ copy).
    assert backend.start_calls[0].env is not None
    # self._env unchanged -- no mutation when env param is None.
    assert session._env is None


async def test_ensure_started_idempotent(_clean_contextvars: None) -> None:
    """A second ``ensure_started(env=...)`` does not trigger a second ``start()``.

    After the first call the backend is alive and ``_needs_restart`` is
    False, so the start path is skipped. env is "injected once at start
    time".
    """
    session, backend = _make_session(env=None)
    env_a = {"MODEX_TASK_ID": "a"}
    env_b = {"MODEX_TASK_ID": "b"}

    await session.ensure_started(env=env_a)
    await session.ensure_started(env=env_b)

    # Only the first call started the backend.
    assert len(backend.start_calls) == 1
    # env_b was silently ignored — self._env retains env_a (spec: "env
    # silently ignored when session already started").
    assert session._env == env_a


async def test_ensure_backend_alive_uses_mutated_env(
    _clean_contextvars: None,
) -> None:
    """After env injection, ``_ensure_backend_alive`` re-starts with the same env.

    Verifies the recovery path: when ``write()`` detects the backend is
    dead, it calls ``_ensure_backend_alive()`` which reads
    ``self._startup_env()`` -> ``self._env`` (mutated by the earlier
    ``ensure_started(env=env_A)``).
    """
    session, backend = _make_session(env=None)
    env_a = {"MODEX_TASK_ID": "a"}

    await session.ensure_started(env=env_a)
    assert len(backend.start_calls) == 1

    # Simulate backend death -- write() will detect this and call
    # _ensure_backend_alive() to restart.
    backend._alive = False
    await session.write("data")

    assert len(backend.start_calls) == 2
    assert backend.start_calls[1].env is not None
    assert backend.start_calls[1].env["MODEX_TASK_ID"] == "a"


# ── CommandTool.execute() env injection ──


async def test_command_tool_injects_env_from_contextvar(
    _clean_contextvars: None,
) -> None:
    """CommandTool reads ``_modex_env`` and injects env, always uses get_default().

    With ``_modex_env={"MODEX_TASK_ID": "t1"}``:
      - session comes from ``get_default()`` (name == "default")
      - ``ensure_started`` receives ``build_full_env({"MODEX_TASK_ID": "t1"})``
      - backend ``start()`` env contains ``MODEX_TASK_ID``
    """
    backend = _FakeBackend()
    manager = _make_manager([backend])
    registry = ProcessRegistry()
    tool = CommandTool(manager=manager, registry=registry, config=_make_config())

    token_env = _modex_env.set({"MODEX_TASK_ID": "t1"})
    try:
        fake_result = PollResult(
            outcome=PollOutcome.PROMPT_DETECTED,
            output_parts=["hi"],
            elapsed_ms=10,
        )
        with patch(
            "modex_agent.tools.terminal.poll_loop.poll_until_settled",
            new=AsyncMock(return_value=fake_result),
        ):
            await tool.execute(command="echo hi")
    finally:
        _modex_env.reset(token_env)

    assert backend.start_calls, "expected start() to be called"
    session = manager.get("default")
    assert session is not None
    assert session.name == "default"

    assert backend.start_calls[0].env is not None
    assert backend.start_calls[0].env["MODEX_TASK_ID"] == "t1"


async def test_command_tool_fallback_no_contextvar(
    _clean_contextvars: None,
) -> None:
    """Without contextvars, CommandTool falls back to ``get_default()`` + ``env=None``.

    Behavior identical to before the env-injection wiring: session name
    is "default" (from ``get_default`` -> ``get_or_create("default")``),
    and ``ensure_started`` receives ``env=None`` (no MODEX_ vars injected,
    ``session._env`` stays None).
    """
    backend = _FakeBackend()
    manager = _make_manager([backend])
    registry = ProcessRegistry()
    tool = CommandTool(manager=manager, registry=registry, config=_make_config())

    # Defensive: no contextvars set.
    assert _modex_env.get() is None
    assert _current_session_id.get() is None

    fake_result = PollResult(
        outcome=PollOutcome.PROMPT_DETECTED,
        output_parts=["hi"],
        elapsed_ms=10,
    )
    with patch(
        "modex_agent.tools.terminal.poll_loop.poll_until_settled",
        new=AsyncMock(return_value=fake_result),
    ):
        await tool.execute(command="echo hi")

    # Session was created via get_default() -> get_or_create("default").
    assert backend.start_calls, "expected start() to be called"
    session = manager.get("default")
    assert session is not None
    assert session.name == "default"

    # env=None passed to ensure_started -> self._env not mutated (stays None).
    assert session._env is None
    # And the start env has no MODEX_TASK_ID (the var test 5 injects).
    start_env = backend.start_calls[0].env
    assert start_env is not None
    assert "MODEX_TASK_ID" not in start_env
