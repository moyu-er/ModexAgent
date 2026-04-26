"""Tests for StorageLock abstraction and implementations."""

import asyncio

import pytest

from framework.memory.core.lock import (
    AioRWLock,
    FileStorageLock,
    NoOpStorageLock,
)


@pytest.mark.asyncio
class TestAioRWLock:
    async def test_basic_read_write(self):
        lock = AioRWLock()
        acquired = []

        async with lock.write():
            acquired.append("write")

        async with lock.read():
            acquired.append("read")

        assert acquired == ["write", "read"]

    async def test_writer_reentrancy(self):
        lock = AioRWLock()
        async with lock.write(), lock.write(), lock.write():
            pass

    async def test_writer_can_read_without_deadlock(self):
        lock = AioRWLock()
        async with lock.write(), lock.read():
            pass

    async def test_concurrent_readers(self):
        lock = AioRWLock()
        barrier = asyncio.Event()
        count = 0

        async def reader():
            nonlocal count
            async with lock.read():
                count += 1
                if count == 2:
                    barrier.set()
                await asyncio.sleep(0.05)

        await asyncio.gather(reader(), reader())
        assert count == 2

    async def test_writer_blocks_readers(self):
        lock = AioRWLock()
        order = []

        async def writer():
            async with lock.write():
                order.append("write_start")
                await asyncio.sleep(0.05)
                order.append("write_end")

        async def reader():
            await asyncio.sleep(0.01)
            async with lock.read():
                order.append("read")

        await asyncio.gather(writer(), reader())
        assert order == ["write_start", "write_end", "read"]

    async def test_write_reentrancy_depth_tracking(self):
        lock = AioRWLock()
        async with lock.write():
            assert lock._writer_depth == 1
            async with lock.write():
                assert lock._writer_depth == 2
                async with lock.write():
                    assert lock._writer_depth == 3
                assert lock._writer_depth == 2
            assert lock._writer_depth == 1
        assert lock._writer_depth == 0
        assert lock._writer_task is None

    async def test_release_write_by_non_holder_raises(self):
        lock = AioRWLock()
        async with lock.write():
            pass
        with pytest.raises(RuntimeError, match="Cannot release a write lock"):
            await lock.release_write()

    async def test_release_read_by_non_holder_raises(self):
        lock = AioRWLock()
        async with lock.read():
            pass
        with pytest.raises(RuntimeError, match="Cannot release a read lock"):
            await lock.release_read()

    async def test_read_timeout_when_writer_held(self):
        lock = AioRWLock()
        writer_started = asyncio.Event()

        async def writer():
            async with lock.write():
                writer_started.set()
                await asyncio.sleep(0.05)

        task = asyncio.create_task(writer())
        await writer_started.wait()
        with pytest.raises(TimeoutError):
            async with lock.read(timeout=0.01):
                pass
        await task

    async def test_write_timeout_when_reader_held(self):
        lock = AioRWLock()
        reader_started = asyncio.Event()

        async def reader():
            async with lock.read():
                reader_started.set()
                await asyncio.sleep(0.05)

        task = asyncio.create_task(reader())
        await reader_started.wait()
        with pytest.raises(TimeoutError):
            async with lock.write(timeout=0.01):
                pass
        await task


@pytest.mark.asyncio
class TestNoOpStorageLock:
    async def test_noop_does_not_block(self):
        lock = NoOpStorageLock()
        order = []

        async def task1():
            async with lock.write():
                order.append("w1")
                await asyncio.sleep(0.02)

        async def task2():
            async with lock.write():
                order.append("w2")

        await asyncio.gather(task1(), task2())
        assert "w1" in order and "w2" in order

    async def test_noop_accepts_timeout_parameter(self):
        lock = NoOpStorageLock()
        async with lock.read(timeout=0.01):
            pass
        async with lock.write(timeout=0.01):
            pass


@pytest.mark.asyncio
class TestFileStorageLock:
    async def test_file_lock_basic(self, tmp_path):
        lock_file = tmp_path / "test.lock"
        lock = FileStorageLock(lock_file)

        async with lock.write():
            pass

        async with lock.read():
            pass

    async def test_file_lock_excludes_concurrent_writers(self, tmp_path):
        import sys

        if sys.platform == "win32":
            pytest.skip("Windows file locks can deadlock within the same process")

        lock_file = tmp_path / "test.lock"
        lock1 = FileStorageLock(lock_file)
        lock2 = FileStorageLock(lock_file)
        order = []

        async def writer1():
            async with lock1.write():
                order.append("w1_start")
                await asyncio.sleep(0.02)
                order.append("w1_end")

        async def writer2():
            await asyncio.sleep(0.005)
            async with lock2.write():
                order.append("w2_start")
                order.append("w2_end")

        await asyncio.gather(writer1(), writer2())
        assert order == ["w1_start", "w1_end", "w2_start", "w2_end"]
