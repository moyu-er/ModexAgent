"""DreamScanner — periodic background consolidation scan."""
import asyncio
import pytest
from modex_agent.pipeline.dream_scanner import DreamScanner


class _FakeCtx:
    def __init__(self, session_id, user_id=None, tenant_id=None):
        self.session_id = session_id
        self.user_id = user_id
        self.tenant_id = tenant_id


class _FakeMemorySystem:
    def __init__(self, counts):
        self._counts = counts

    async def get_unprocessed_history_count(self, ctx):
        return self._counts.get(ctx.session_id, 0)


class _FakeCtxMgr:
    def __init__(self, contexts, memory_system):
        self._contexts = contexts
        self.memory_system = memory_system

    def get_active_contexts(self):
        return list(self._contexts)


async def test_scanner_triggers_engine_when_count_positive():
    ctx = _FakeCtx("s1", "u1")
    mem = _FakeMemorySystem({"s1": 3})

    class _Eng:
        def __init__(self):
            self.run_calls = []

        async def run(self, c):
            self.run_calls.append(c.session_id)

    eng = _Eng()
    cmgr = _FakeCtxMgr([ctx], mem)
    scanner = DreamScanner(dream_engine=eng, dream_interval=0.01, context_manager=cmgr)

    task = asyncio.create_task(scanner.run_forever())
    await asyncio.sleep(0.05)
    scanner.stop()
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert "s1" in eng.run_calls


async def test_scanner_noop_without_duck_typed_methods():
    class _Bare:
        pass
    scanner = DreamScanner(dream_engine=object(), dream_interval=0.01, context_manager=_Bare())
    task = asyncio.create_task(scanner.run_forever())
    await asyncio.sleep(0.03)
    scanner.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # no error raised — duck-typing getattr guards returned None, loop continued
    assert task.cancelled() or task.done()


async def test_scanner_skips_zero_count():
    ctx = _FakeCtx("s1")
    mem = _FakeMemorySystem({"s1": 0})

    class _Eng:
        def __init__(self):
            self.run_calls = []

        async def run(self, c):
            self.run_calls.append(c.session_id)

    eng = _Eng()
    cmgr = _FakeCtxMgr([ctx], mem)
    scanner = DreamScanner(dream_engine=eng, dream_interval=0.01, context_manager=cmgr)
    task = asyncio.create_task(scanner.run_forever())
    await asyncio.sleep(0.05)
    scanner.stop()
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert eng.run_calls == []  # count 0 → not triggered
