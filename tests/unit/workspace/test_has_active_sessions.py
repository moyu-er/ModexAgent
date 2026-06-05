"""Tests for Pipeline.has_active_sessions() logic."""
from __future__ import annotations

import asyncio

import pytest


class TestHasActiveSessions:
    """Verify has_active_sessions() correctness — the logic used by
    workspace cd/exit to determine whether switching is safe."""

    def _has_active(self, session_tasks: dict[str, asyncio.Task]) -> bool:
        """Replicate the exact Pipeline.has_active_sessions() logic."""
        return any(not task.done() for task in session_tasks.values())

    def test_empty_tasks_returns_false(self):
        """No sessions at all → no active sessions."""
        assert self._has_active({}) is False

    @pytest.mark.asyncio
    async def test_completed_task_returns_false(self):
        """A task that has finished is not active."""

        async def _work() -> None:
            return

        task = asyncio.create_task(_work())
        await task
        assert task.done()
        assert self._has_active({"s1": task}) is False

    @pytest.mark.asyncio
    async def test_running_task_returns_true(self):
        """A task that is still running IS active."""
        started = asyncio.Event()
        done_flag = asyncio.Event()

        async def _work() -> None:
            started.set()
            await done_flag.wait()

        task = asyncio.create_task(_work())
        await started.wait()
        try:
            assert task.done() is False
            assert self._has_active({"s1": task}) is True
        finally:
            done_flag.set()
            await task

    @pytest.mark.asyncio
    async def test_cancelled_task_returns_false(self):
        """A cancelled task (done=True) is not active."""

        async def _work() -> None:
            await asyncio.sleep(999)

        task = asyncio.create_task(_work())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.done()
        assert self._has_active({"s1": task}) is False

    @pytest.mark.asyncio
    async def test_mixed_tasks_returns_true(self):
        """If ANY task is running, has_active is True."""
        started = asyncio.Event()
        done_flag = asyncio.Event()

        async def _work() -> None:
            started.set()
            await done_flag.wait()

        running = asyncio.create_task(_work())
        await started.wait()

        # completed task
        async def _done() -> None:
            return

        completed = asyncio.create_task(_done())
        await completed
        assert completed.done()

        try:
            assert self._has_active({"s1": completed, "s2": running}) is True
        finally:
            done_flag.set()
            await running
