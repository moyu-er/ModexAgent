import asyncio

import pytest

from modex_agent.memory.hooks import (
    CleanupFinishedHook,
    CleanupTriggeredHook,
    MemoryHookContext,
    MemoryHookPoint,
    MemoryHookRunner,
)


class RecordingHook(CleanupTriggeredHook, CleanupFinishedHook):
    def __init__(self) -> None:
        self.calls: list[MemoryHookPoint] = []

    async def on_cleanup_triggered(self, ctx: MemoryHookContext) -> None:
        self.calls.append(MemoryHookPoint.CLEANUP_TRIGGERED)

    async def on_cleanup_finished(self, ctx: MemoryHookContext) -> None:
        self.calls.append(MemoryHookPoint.CLEANUP_FINISHED)


class RegisteringHook(CleanupTriggeredHook):
    def __init__(self, runner: MemoryHookRunner, hook: CleanupTriggeredHook) -> None:
        self._runner = runner
        self._hook = hook
        self.calls = 0

    async def on_cleanup_triggered(self, ctx: MemoryHookContext) -> None:
        self.calls += 1
        if self.calls == 1:
            self._runner.add(self._hook)


class TriggeredRecordingHook(CleanupTriggeredHook):
    def __init__(self) -> None:
        self.calls = 0

    async def on_cleanup_triggered(self, ctx: MemoryHookContext) -> None:
        self.calls += 1


class SleepingHook(CleanupTriggeredHook):
    async def on_cleanup_triggered(self, ctx: MemoryHookContext) -> None:
        await asyncio.sleep(100)


class RaisingHook(CleanupTriggeredHook):
    async def on_cleanup_triggered(self, ctx: MemoryHookContext) -> None:
        raise RuntimeError("boom")


class CancelledHook(CleanupTriggeredHook):
    async def on_cleanup_triggered(self, ctx: MemoryHookContext) -> None:
        raise asyncio.CancelledError


async def test_multi_inheritance_hook_records_each_point_once() -> None:
    hook = RecordingHook()
    runner = MemoryHookRunner()
    runner.add(hook)
    ctx = MemoryHookContext()

    await runner.dispatch(MemoryHookPoint.CLEANUP_TRIGGERED, ctx)
    await runner.dispatch(MemoryHookPoint.CLEANUP_FINISHED, ctx)

    assert hook.calls == [
        MemoryHookPoint.CLEANUP_TRIGGERED,
        MemoryHookPoint.CLEANUP_FINISHED,
    ]


async def test_registration_during_dispatch_applies_to_next_dispatch() -> None:
    late_hook = TriggeredRecordingHook()
    runner = MemoryHookRunner()
    registering_hook = RegisteringHook(runner, late_hook)
    runner.add(registering_hook)
    ctx = MemoryHookContext()

    await runner.dispatch(MemoryHookPoint.CLEANUP_TRIGGERED, ctx)

    assert registering_hook.calls == 1
    assert late_hook.calls == 0

    await runner.dispatch(MemoryHookPoint.CLEANUP_TRIGGERED, ctx)

    assert registering_hook.calls == 2
    assert late_hook.calls == 1


async def test_timeout_does_not_block_subsequent_hook() -> None:
    recording_hook = TriggeredRecordingHook()
    runner = MemoryHookRunner()
    runner.add(SleepingHook())
    runner.add(recording_hook)

    await runner.dispatch(
        MemoryHookPoint.CLEANUP_TRIGGERED,
        MemoryHookContext(),
        timeout=0.01,
    )

    assert recording_hook.calls == 1


async def test_exception_does_not_block_subsequent_hook() -> None:
    recording_hook = TriggeredRecordingHook()
    runner = MemoryHookRunner()
    runner.add(RaisingHook())
    runner.add(recording_hook)

    await runner.dispatch(MemoryHookPoint.CLEANUP_TRIGGERED, MemoryHookContext())

    assert recording_hook.calls == 1


async def test_cancellation_propagates() -> None:
    runner = MemoryHookRunner()
    runner.add(CancelledHook())

    with pytest.raises(asyncio.CancelledError):
        await runner.dispatch(MemoryHookPoint.CLEANUP_TRIGGERED, MemoryHookContext())
