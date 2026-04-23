"""Storage lock abstraction layer."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path
from types import TracebackType
from typing import Any, AsyncContextManager


class StorageLock(ABC):
    """Abstract base class for storage-level locks.

    Provides async context managers for read and write access.
    """

    @abstractmethod
    def read(self) -> AsyncContextManager[StorageLockContext]:
        """Return an async context manager for read access."""
        pass

    @abstractmethod
    def write(self) -> AsyncContextManager[StorageLockContext]:
        """Return an async context manager for write access."""
        pass


class StorageLockContext(AsyncContextManager["StorageLockContext"]):
    """Async context manager returned by StorageLock.read() / write()."""

    async def __aenter__(self) -> StorageLockContext:
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.release()

    @abstractmethod
    async def acquire(self) -> None:
        pass

    @abstractmethod
    async def release(self) -> None:
        pass


class _AioRWLockReadContext(StorageLockContext):
    def __init__(self, lock: AioRWLock) -> None:
        self._lock = lock

    async def acquire(self) -> None:
        await self._lock.acquire_read()

    async def release(self) -> None:
        await self._lock.release_read()


class _AioRWLockWriteContext(StorageLockContext):
    def __init__(self, lock: AioRWLock) -> None:
        self._lock = lock

    async def acquire(self) -> None:
        await self._lock.acquire_write()

    async def release(self) -> None:
        await self._lock.release_write()


class AioRWLock(StorageLock):
    """Writer-reentrant RWLock based on aiorwlock.

    Tracks the writer task using asyncio.current_task() to allow the same
    task to re-enter the write lock. If the current task already holds the
    write lock, acquiring the read lock is a no-op.
    """

    def __init__(self) -> None:
        import aiorwlock

        self._rwlock = aiorwlock.RWLock()
        self._writer_task: asyncio.Task[Any] | None = None
        self._writer_depth: int = 0
        self._reader_tasks: set[asyncio.Task[Any]] = set()
        self._reader_depths: dict[asyncio.Task[Any], int] = {}

    async def acquire_read(self) -> None:
        current = asyncio.current_task()
        if self._writer_task is not None and self._writer_task == current:
            return
        await self._rwlock.reader_lock.acquire()
        if current is not None:
            self._reader_tasks.add(current)
            self._reader_depths[current] = self._reader_depths.get(current, 0) + 1

    async def release_read(self) -> None:
        current = asyncio.current_task()
        if self._writer_task is not None and self._writer_task == current:
            return
        if current is None or current not in self._reader_tasks:
            raise RuntimeError("Cannot release a read lock not held by the current task")
        # Release the underlying aiorwlock read lock on every call to keep
        # its internal reader count in sync with our depth tracking.
        self._rwlock.reader_lock.release()
        depth = self._reader_depths.get(current, 1) - 1
        if depth <= 0:
            self._reader_tasks.discard(current)
            self._reader_depths.pop(current, None)
        else:
            self._reader_depths[current] = depth

    async def acquire_write(self) -> None:
        current = asyncio.current_task()
        if self._writer_task is not None and self._writer_task == current:
            self._writer_depth += 1
            return
        await self._rwlock.writer_lock.acquire()
        self._writer_task = current
        self._writer_depth = 1

    async def release_write(self) -> None:
        current = asyncio.current_task()
        if self._writer_task is not None and self._writer_task == current:
            self._writer_depth -= 1
            if self._writer_depth == 0:
                self._writer_task = None
                self._rwlock.writer_lock.release()
            return
        # Prevent illegal release of a lock not held by the current task
        raise RuntimeError("Cannot release a write lock not held by the current task")

    def read(self) -> StorageLockContext:
        return _AioRWLockReadContext(self)

    def write(self) -> StorageLockContext:
        return _AioRWLockWriteContext(self)


class NoOpStorageLock(StorageLock):
    """Zero-overhead null implementation for single-threaded use."""

    class _NoOpContext(StorageLockContext):
        async def acquire(self) -> None:
            pass

        async def release(self) -> None:
            pass

    def read(self) -> StorageLockContext:
        return self._NoOpContext()

    def write(self) -> StorageLockContext:
        return self._NoOpContext()


class FileStorageLock(StorageLock):
    """Cross-process file-based lock using filelock.

    Uses an exclusive file lock for both read and write operations.
    Writer-reentrant within the same asyncio task to prevent deadlocks
    when storage methods call each other while holding the lock.
    filelock is imported lazily so it remains an optional dependency.
    """

    def __init__(self, lock_file: Path | str) -> None:
        try:
            import filelock
        except ImportError as exc:
            raise ImportError(
                "filelock is required for FileStorageLock. "
                "Install it with: pip install filelock"
            ) from exc
        self._lock_file = Path(lock_file)
        self._lock = filelock.FileLock(str(self._lock_file))
        self._writer_task: asyncio.Task[Any] | None = None
        self._writer_depth: int = 0

    class _FileLockContext(StorageLockContext):
        def __init__(self, owner: FileStorageLock, is_write: bool) -> None:
            self._owner = owner
            self._is_write = is_write
            self._acquired = False

        async def acquire(self) -> None:
            current = asyncio.current_task()
            if self._is_write:
                if self._owner._writer_task is not None and self._owner._writer_task == current:
                    self._owner._writer_depth += 1
                    return
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._owner._lock.acquire)
                self._owner._writer_task = current
                self._owner._writer_depth = 1
                self._acquired = True
            else:
                if self._owner._writer_task is not None and self._owner._writer_task == current:
                    return
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._owner._lock.acquire)
                self._acquired = True

        async def release(self) -> None:
            current = asyncio.current_task()
            if self._is_write:
                if self._owner._writer_task is not None and self._owner._writer_task == current:
                    self._owner._writer_depth -= 1
                    if self._owner._writer_depth == 0:
                        self._owner._writer_task = None
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, self._owner._lock.release)
                    return
            if self._acquired:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._owner._lock.release)
                self._acquired = False

    def read(self) -> StorageLockContext:
        return self._FileLockContext(self, is_write=False)

    def write(self) -> StorageLockContext:
        return self._FileLockContext(self, is_write=True)
