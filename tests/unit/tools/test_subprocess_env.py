"""Tests for SubprocessExecutor env-override reading from ``_modex_env``.

Verifies the three acceptance criteria from the spec:

1. ``test_no_contextvar_uses_default_env`` -- with the contextvar unset
   (default None), the env passed to ``asyncio.create_subprocess_shell``
   equals ``build_full_env()`` (zero-behaviour-change).
2. ``test_contextvar_overrides_injected`` -- when the env-injection hook
   sets ``_modex_env`` to ``{"MODEX_TASK_ID": "test-123"}``, that pair
   appears in the env passed to the subprocess.
3. ``test_contextvar_none_equals_no_overrides`` -- explicitly setting
   ``_modex_env`` to ``None`` is equivalent to the default (no MODEX_
   vars, env equals ``build_full_env()``).

The mock patches ``asyncio.create_subprocess_shell`` at the module where
it is used (``modex_agent.tools.terminal.subprocess_tool.asyncio``) so no
real subprocess is spawned. ``AsyncMock`` makes the awaited call return a
fake Process whose ``communicate()`` resolves immediately.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modex_agent.runtime.env_context import _modex_env
from modex_agent.tools.terminal.env import build_full_env
from modex_agent.tools.terminal.subprocess_tool import SubprocessExecutor


def _make_fake_process(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    """Build a fake asyncio.subprocess.Process-like object.

    ``communicate`` is an ``AsyncMock`` because ``execute()`` awaits it.
    ``returncode`` and ``kill()`` mirror the real Process surface used by
    the executor's success and timeout paths.
    """
    process = MagicMock()
    process.communicate = AsyncMock(return_value=(stdout, stderr))
    process.returncode = returncode
    return process


@pytest.fixture
def patched_create_subprocess_shell() -> Iterator[AsyncMock]:
    """Patch ``asyncio.create_subprocess_shell`` in subprocess_tool.

    Yields the AsyncMock so individual tests can inspect ``call_args``.
    The mock's ``return_value`` is a fresh fake process per test.
    """
    fake_process = _make_fake_process()
    mock = AsyncMock(return_value=fake_process)
    with patch(
        "modex_agent.tools.terminal.subprocess_tool.asyncio.create_subprocess_shell",
        new=mock,
    ):
        yield mock


class TestSubprocessExecutorEnvFromContextVar:
    """Verify SubprocessExecutor.execute() reads env overrides from _modex_env."""

    async def test_no_contextvar_uses_default_env(
        self,
        patched_create_subprocess_shell: AsyncMock,
    ) -> None:
        """Without setting _modex_env, env kwarg equals build_full_env().

        This pins the zero-behaviour-change invariant: when no hook has
        populated the contextvar, the executor behaves exactly as it did
        before the env-injection wiring was added.
        """
        # Defensive: confirm the default state at test entry.
        assert _modex_env.get() is None

        executor = SubprocessExecutor()
        await executor.execute("echo hi")

        assert patched_create_subprocess_shell.await_count == 1
        env_passed = patched_create_subprocess_shell.call_args.kwargs["env"]
        assert env_passed == build_full_env()
        # Sanity: the default env carries no MODEX_ task-scoped vars.
        assert not any(k.startswith("MODEX_") for k in env_passed)

    async def test_contextvar_overrides_injected(
        self,
        patched_create_subprocess_shell: AsyncMock,
    ) -> None:
        """_modex_env.set({"MODEX_TASK_ID": "test-123"}) is propagated to the
        subprocess env kwarg verbatim."""
        token = _modex_env.set({"MODEX_TASK_ID": "test-123"})
        try:
            executor = SubprocessExecutor()
            await executor.execute("echo hi")

            assert patched_create_subprocess_shell.await_count == 1
            env_passed = patched_create_subprocess_shell.call_args.kwargs["env"]
            assert env_passed["MODEX_TASK_ID"] == "test-123"
        finally:
            _modex_env.reset(token)

        # Cleanup verified: contextvar restored to default None.
        assert _modex_env.get() is None

    async def test_contextvar_none_equals_no_overrides(
        self,
        patched_create_subprocess_shell: AsyncMock,
    ) -> None:
        """Explicitly setting _modex_env to None equals the default behaviour:
        env kwarg equals build_full_env() and contains no MODEX_ vars."""
        token = _modex_env.set(None)
        try:
            executor = SubprocessExecutor()
            await executor.execute("echo hi")

            assert patched_create_subprocess_shell.await_count == 1
            env_passed = patched_create_subprocess_shell.call_args.kwargs["env"]
            assert env_passed == build_full_env()
            assert not any(k.startswith("MODEX_") for k in env_passed)
        finally:
            _modex_env.reset(token)

        assert _modex_env.get() is None
